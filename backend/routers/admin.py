"""Admin-only endpoints: users, GPU assignments, sessions, usage and audit."""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import case, func
from sqlalchemy.orm import Session

import models
from auth import (
    apply_derived_credentials,
    derive_credentials,
    get_password_hash,
    require_admin,
    revoke_tokens,
    validate_password,
)
from config import settings
from database import get_db
from schemas import (
    GpuAssignmentCreate,
    GpuAssignmentResponse,
    GpuAssignmentUpdate,
    MessageResponse,
    UserCreate,
    UserResponse,
    UserUpdate,
    UserWithAssignment,
)
from schemas import JupyterSessionResponse
from services import (
    audit, container_manager, metrics, quota, ratelimit, session_backend, usage,
)

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

@router.get("/users", response_model=List[UserWithAssignment])
async def list_users(db: Session = Depends(get_db)):
    """All platform users with their GPU assignment embedded."""
    return (
        db.query(models.User)
        .filter(models.User.deleted_at.is_(None))
        .order_by(models.User.id)
        .all()
    )


@router.get("/users/{user_id}", response_model=UserWithAssignment)
async def get_user(user_id: int, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(
        models.User.id == user_id, models.User.deleted_at.is_(None)
    ).first()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user


@router.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreate,
    request: Request,
    db: Session = Depends(get_db),
    admin: models.User = Depends(require_admin),
):
    """Create a new platform account."""
    # A trashed account still owns its username and email.  That is the point:
    # releasing the name would let the next account created with it inherit the
    # previous person's files.
    clash = db.query(models.User).filter(models.User.username == payload.username).first()
    if clash is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Username {payload.username!r} is in the trash. Restore that account "
                "or delete it permanently before reusing the name."
                if clash.deleted_at is not None else "Username already exists"
            ),
        )
    clash = db.query(models.User).filter(models.User.email == payload.email).first()
    if clash is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"That email belongs to {clash.username!r}, which is in the trash."
                if clash.deleted_at is not None else "Email already exists"
            ),
        )
    validate_password(payload.password)

    user = models.User(
        username=payload.username,
        email=payload.email,
        full_name=payload.full_name,
        is_admin=payload.is_admin,
        hashed_password=get_password_hash(payload.password),
        # Jupyter + UNIX credentials derived from the same password
        **derive_credentials(payload.password),
        disk_quota_mb=payload.disk_quota_mb,
        gpu_hours_quota=payload.gpu_hours_quota,
        cpu_hours_quota=payload.cpu_hours_quota,
        home_path=(
            __import__("services.workspaces", fromlist=["validate_home_path"])
            .validate_home_path(payload.home_path) or None
        ) if payload.home_path else None,
    )
    db.add(user)
    audit.record(
        db, "user.create", actor=admin.username, target=payload.username,
        ip_address=audit.client_ip(request), commit=False,
    )
    db.commit()
    db.refresh(user)
    return user


@router.put("/users/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: int,
    payload: UserUpdate,
    request: Request,
    db: Session = Depends(get_db),
    admin: models.User = Depends(require_admin),
):
    """Partially update a user (email, name, admin/active flags, quotas)."""
    user = db.query(models.User).filter(
        models.User.id == user_id, models.User.deleted_at.is_(None)
    ).first()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    changes = []
    if payload.email is not None:
        clash = (
            db.query(models.User)
            .filter(models.User.email == payload.email, models.User.id != user_id)
            .first()
        )
        if clash:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already in use")
        user.email = payload.email
        changes.append("email")
    if payload.full_name is not None:
        user.full_name = payload.full_name
        changes.append("full_name")
    if payload.is_admin is not None:
        if user.username == "admin" and payload.is_admin is False:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot revoke admin from the built-in admin account.",
            )
        user.is_admin = payload.is_admin
        # Privilege changes must not wait for the old token to expire.
        revoke_tokens(user)
        changes.append(f"is_admin={payload.is_admin}")
    if payload.is_active is not None:
        if user.username == "admin" and payload.is_active is False:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot deactivate the built-in admin account.",
            )
        user.is_active = payload.is_active
        # Deactivation used to leave the user working for up to a full token
        # lifetime; revoking cuts them off on their next request.
        revoke_tokens(user)
        changes.append(f"is_active={payload.is_active}")
    if payload.disk_quota_mb is not None:
        user.disk_quota_mb = payload.disk_quota_mb or None
        changes.append(f"disk_quota_mb={payload.disk_quota_mb}")
        # Republish it so the user's shell reports the new figure at once,
        # rather than the one their session was created with.
        container_manager.write_platform_limits_file(
            user.username, quota.disk_quota_mb(user)
        )
    if payload.gpu_hours_quota is not None:
        user.gpu_hours_quota = payload.gpu_hours_quota or None
        changes.append(f"gpu_hours_quota={payload.gpu_hours_quota}")
    if payload.cpu_hours_quota is not None:
        user.cpu_hours_quota = payload.cpu_hours_quota or None
        changes.append(f"cpu_hours_quota={payload.cpu_hours_quota}")
    if payload.home_path is not None:
        from services import workspaces

        resolved = workspaces.validate_home_path(payload.home_path)
        user.home_path = resolved or None
        changes.append(f"home_path={resolved or '(platform workspace)'}")

    db.add(user)
    audit.record(
        db, "user.update", actor=admin.username, target=user.username,
        detail=", ".join(changes) or "no change",
        ip_address=audit.client_ip(request), commit=False,
    )
    db.commit()
    db.refresh(user)
    return user


@router.delete("/users/{user_id}", response_model=MessageResponse)
async def delete_user(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: models.User = Depends(require_admin),
):
    """Move a user to the trash.

    Everything they have running is stopped, their workspace is renamed aside
    and their access ends immediately, but the account and its history are
    kept, so the username stays reserved and an administrator can undo this.
    """
    from services import trash

    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if user.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That account is already in the trash.",
        )
    if user.username == "admin":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The built-in admin account cannot be deleted.",
        )
    if user.id == admin.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot delete the account you are signed in with.",
        )

    username = user.username
    outcome = trash.soft_delete(db, user, actor=admin.username, ip_address=audit.client_ip(request))

    note = ""
    if outcome["jobs_cancelled"]:
        note += f" {len(outcome['jobs_cancelled'])} job(s) stopped."
    if outcome["mapped_home_kept"]:
        note += f" Their home directory {outcome['mapped_home_kept']} was left untouched."
    elif outcome["archived_workspace"]:
        note += f" Files kept as {outcome['archived_workspace']}."
    return {"message": f"User {username!r} moved to the trash.{note}"}


# ---------------------------------------------------------------------------
# Trash
# ---------------------------------------------------------------------------

@router.get("/trash")
def list_trash(db: Session = Depends(get_db)):
    """Trashed accounts, and any workspace directory no account owns."""
    from services import trash

    return trash.list_trash(db)


@router.post("/trash/{user_id}/restore", response_model=MessageResponse)
def restore_user(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: models.User = Depends(require_admin),
):
    """Bring a trashed account back, with its files if they are still there."""
    from services import trash

    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user is None or user.deleted_at is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No such account in the trash."
        )

    result = trash.restore(db, user, actor=admin.username, ip_address=audit.client_ip(request))
    detail = {
        "restored": "Their files were restored.",
        "fresh": "They start with a new, empty workspace.",
        "kept": "A workspace with that name already existed, so the archived one was left in the trash.",
    }[result["workspace"]]
    if result["gpu_assignment_restored"]:
        detail += " Their GPU assignment was restored."
    return {"message": f"User {user.username!r} restored. {detail}"}


@router.delete("/trash/{user_id}", response_model=MessageResponse)
def purge_user(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: models.User = Depends(require_admin),
):
    """Delete a trashed account for good, together with its archived files."""
    from services import trash

    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user is None or user.deleted_at is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No such account in the trash."
        )

    username = user.username
    result = trash.purge(db, user, actor=admin.username, ip_address=audit.client_ip(request))
    note = " Their files were deleted." if result["archive_removed"] else ""
    if result["mapped_home_kept"]:
        note += " Their home directory was left untouched."
    return {"message": f"User {username!r} deleted permanently.{note}"}


@router.delete("/trash/directories/{name}", response_model=MessageResponse)
def purge_directory(
    name: str,
    request: Request,
    db: Session = Depends(get_db),
    admin: models.User = Depends(require_admin),
):
    """Delete one workspace directory that no account owns.

    For directories left behind by the older delete, which removed the account
    row and left the files unreachable under the original username.
    """
    from services import trash

    listing = trash.list_trash(db)
    if name not in {entry["name"] for entry in listing["orphans"]}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That directory belongs to an account. Delete the account instead.",
        )
    if not trash.remove_directory(name):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Directory not found."
        )
    audit.record(
        db, "workspace.purge", actor=admin.username, target=name,
        ip_address=audit.client_ip(request),
    )
    return {"message": f"Deleted {name}."}


@router.post("/users/{user_id}/reset-password", response_model=MessageResponse)
async def reset_password(
    user_id: int,
    payload: dict,
    request: Request,
    db: Session = Depends(get_db),
    admin: models.User = Depends(require_admin),
):
    """Admin resets any user's password (revoking their existing sessions)."""
    user = db.query(models.User).filter(
        models.User.id == user_id, models.User.deleted_at.is_(None)
    ).first()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    new_password = payload.get("new_password") if isinstance(payload, dict) else None
    validate_password(str(new_password or ""))

    user.hashed_password = get_password_hash(str(new_password))
    apply_derived_credentials(user, str(new_password))
    revoke_tokens(user)
    db.add(user)
    # Keep a running session's SSH login in step with the new password.
    from services import container_manager, session_backend

    if settings.UNIFIED_PASSWORD and session_backend.active_backend() == "container":
        container_manager.update_container_password(user.username, user.unix_password_hash)
        if not user.hashed_jupyter_password:
            container_manager.write_jupyter_auth_config(
                user.username, user.account_jupyter_hash, password_required=False
            )
    # A job token is a second credential: it lives in a file, not in the
    # password, so revoking every JWT above would leave it working.  Rotate it
    # here, the commands read the file on each run, so a workspace that is
    # open keeps working, while any copy taken before the change stops.
    if settings.JOBS_ENABLED:
        from services import jobs as job_service

        job_service.ensure_job_token(db, user)
    audit.record(
        db, "user.reset_password", actor=admin.username, target=user.username,
        ip_address=audit.client_ip(request), commit=False,
    )
    db.commit()
    return {"message": f"Password for {user.username!r} has been reset."}


# ---------------------------------------------------------------------------
# GPU assignments
# ---------------------------------------------------------------------------

def _host_gpus() -> List[Dict[str, Any]]:
    from services import gpu_monitor

    return gpu_monitor.get_gpu_status(detailed=True)


def _validate_gpu_indices(indices: List[int]) -> None:
    """Ensure the requested indices are well-formed and exist on the host.

    An empty list is valid and means "no GPU": an assignment can exist purely
    to cap someone's CPU and RAM.  Only the contents are checked here, that
    the assignment sets *something* is checked by the caller, which is the one
    that can see the other limits.
    """
    if not indices:
        return
    if any(i < 0 for i in indices):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="GPU indices must be >= 0")
    if len(set(indices)) != len(indices):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Duplicate GPU indices")

    known = {g["index"] for g in _host_gpus()}
    if not known:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "No GPUs are visible to the platform, so none can be assigned. "
                "Check nvidia-smi on the host (set ALLOW_MOCK_GPU=true for a "
                "GPU-less development box)."
            ),
        )
    # Every index is checked, not just the largest, assigning [0, 7] on a
    # 2-GPU host used to succeed because only max() was validated.
    unknown = sorted(set(indices) - known)
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"GPU(s) {unknown} do not exist on this host (available: {sorted(known)}).",
        )


def _require_a_limit(gpu_indices, memory_limit_mb, cpu_cores,
                     cpu_limit_seconds, max_processes=None) -> None:
    """Refuse an assignment that sets nothing at all."""
    if not any((gpu_indices, memory_limit_mb, cpu_cores,
                cpu_limit_seconds, max_processes)):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "An assignment must set at least one of: GPUs, RAM limit, "
                "CPU cores, process limit."
            ),
        )


def _validate_max_processes(value) -> None:
    """A process ceiling low enough to be unusable is a mistake, not a policy.

    A workspace needs a few processes just to exist, sshd, the Jupyter server
    and a kernel, so anything under about 16 locks the user out of their own
    session with an error that looks like the platform is broken.
    """
    if value is None:
        return
    if value < 16:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "The process limit must be at least 16, because a workspace needs a "
                "few processes just to start."
            ),
        )


@router.post("/gpu/processes/{pid}/stop", response_model=MessageResponse)
async def stop_gpu_process(
    pid: int,
    request: Request,
    force: bool = Query(False, description="SIGKILL instead of SIGTERM"),
    db: Session = Depends(get_db),
    admin: models.User = Depends(require_admin),
):
    """Stop one process that is holding GPU memory.

    Reaching for a single process rather than the whole workspace is the point:
    a runaway training run can be stopped without taking away the notebook the
    user would fix it in.
    """
    from services import gpu_monitor

    result = gpu_monitor.stop_compute_process(pid, force=force)
    audit.record(
        db, "gpu.process_stopped", actor=admin.username, target=result["user"],
        detail=f"pid={pid} signal={result['signal']} "
               f"mem={result['memory_freed_mb']}MB cmd={result['command'][:120]}",
        ip_address=audit.client_ip(request),
    )
    return {"message": f"Sent {result['signal']} to process {pid} ({result['user']})."}


@router.get("/gpu/assignments", response_model=List[GpuAssignmentResponse])
async def list_assignments(db: Session = Depends(get_db)):
    return db.query(models.GpuAssignment).order_by(models.GpuAssignment.id).all()


@router.post("/gpu/assignments", response_model=GpuAssignmentResponse, status_code=status.HTTP_201_CREATED)
async def create_assignment(
    payload: GpuAssignmentCreate,
    request: Request,
    db: Session = Depends(get_db),
    admin: models.User = Depends(require_admin),
):
    """Assign one or more GPUs to a user (one assignment per user)."""
    user = db.query(models.User).filter(
        models.User.id == payload.user_id, models.User.deleted_at.is_(None)
    ).first()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    _validate_gpu_indices(payload.gpu_indices)
    _validate_max_processes(payload.max_processes)
    _require_a_limit(
        payload.gpu_indices, payload.memory_limit_mb,
        payload.cpu_cores, payload.cpu_limit_seconds, payload.max_processes,
    )

    existing = (
        db.query(models.GpuAssignment)
        .filter(models.GpuAssignment.user_id == payload.user_id)
        .first()
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User already has a GPU assignment. Use PUT to update it.",
        )

    assignment = models.GpuAssignment(
        user_id=payload.user_id,
        gpu_indices=",".join(str(i) for i in sorted(payload.gpu_indices)),
        memory_limit_mb=payload.memory_limit_mb,
        cpu_cores=payload.cpu_cores,
        cpu_limit_seconds=payload.cpu_limit_seconds,
        max_processes=payload.max_processes,
    )
    db.add(assignment)
    audit.record(
        db, "gpu.assign", actor=admin.username, target=user.username,
        detail=f"gpus={sorted(payload.gpu_indices)}",
        ip_address=audit.client_ip(request), commit=False,
    )
    db.commit()
    db.refresh(assignment)
    return assignment


@router.put("/gpu/assignments/{assignment_id}", response_model=GpuAssignmentResponse)
async def update_assignment(
    assignment_id: int,
    payload: GpuAssignmentUpdate,
    request: Request,
    db: Session = Depends(get_db),
    admin: models.User = Depends(require_admin),
):
    assignment = (
        db.query(models.GpuAssignment).filter(models.GpuAssignment.id == assignment_id).first()
    )
    if assignment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assignment not found")

    # Sent-vs-omitted, not null-vs-not-null: the admin form posts every field
    # on every save, so a field cleared there arrives as null and has to clear
    # the limit.  Keying on `is not None` meant a limit could be raised but
    # never removed.
    sent = payload.model_fields_set
    if "gpu_indices" in sent:
        indices = payload.gpu_indices or []
        _validate_gpu_indices(indices)
        assignment.gpu_indices = ",".join(str(i) for i in sorted(indices))
    if "memory_limit_mb" in sent:
        assignment.memory_limit_mb = payload.memory_limit_mb or None
    if "cpu_cores" in sent:
        assignment.cpu_cores = payload.cpu_cores or None
    if "cpu_limit_seconds" in sent:
        assignment.cpu_limit_seconds = payload.cpu_limit_seconds or None
    if "max_processes" in sent:
        _validate_max_processes(payload.max_processes or None)
        assignment.max_processes = payload.max_processes or None

    _require_a_limit(
        assignment.gpu_indices, assignment.memory_limit_mb,
        assignment.cpu_cores, assignment.cpu_limit_seconds,
        assignment.max_processes,
    )

    db.add(assignment)
    audit.record(
        db, "gpu.reassign", actor=admin.username, target=assignment.user.username,
        detail=(f"gpus={assignment.gpu_indices} mem={assignment.memory_limit_mb} "
                f"cores={assignment.cpu_cores} pids={assignment.max_processes}"),
        ip_address=audit.client_ip(request), commit=False,
    )
    db.commit()
    db.refresh(assignment)
    return assignment


@router.delete("/gpu/assignments/{assignment_id}", response_model=MessageResponse)
async def delete_assignment(
    assignment_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: models.User = Depends(require_admin),
):
    """Remove a user's GPU access (their CUDA context reports 0 devices)."""
    assignment = (
        db.query(models.GpuAssignment).filter(models.GpuAssignment.id == assignment_id).first()
    )
    if assignment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assignment not found")

    audit.record(
        db, "gpu.unassign", actor=admin.username, target=assignment.user.username,
        ip_address=audit.client_ip(request), commit=False,
    )
    db.delete(assignment)
    db.commit()
    return {"message": "GPU assignment removed."}


# ---------------------------------------------------------------------------
# Jupyter sessions (admin oversight + force-stop)
# ---------------------------------------------------------------------------

def _session_view(session: models.JupyterSession) -> Dict[str, Any]:
    """Serialize a session row, reconciling dead processes/containers."""
    if session.container_id:
        running = session_backend.is_alive(session.user.username, None, session.container_id)
    else:
        running = session_backend.is_alive(session.user.username, session.pid, None)
    view = JupyterSessionResponse.model_validate(session).model_dump(mode="json")
    view["status"] = "running" if (running and session.status == models.SessionStatus.running) else (
        "stopped" if not running else session.status.value
    )
    # Session secrets belong to their owner only, never to another admin.
    view["token"] = "***"
    view["ssh_password"] = "***"
    view["resources"] = metrics.for_user(session.user.username)
    return view


@router.get("/jupyter/sessions")
def list_sessions(db: Session = Depends(get_db)):
    """Every Jupyter session with owning-user info and live status."""
    sessions = db.query(models.JupyterSession).order_by(models.JupyterSession.id).all()
    result = []
    for session in sessions:
        view = _session_view(session)
        view["user"] = UserResponse.model_validate(session.user).model_dump(mode="json")
        result.append(view)
    return result


@router.post("/jupyter/sessions/{user_id}/stop", response_model=MessageResponse)
def stop_user_session(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: models.User = Depends(require_admin),
):
    """Force-stop a user's JupyterLab session."""
    session = (
        db.query(models.JupyterSession).filter(models.JupyterSession.user_id == user_id).first()
    )
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    if session.container_id:
        stopped = session_backend.stop_session(session.user.username, None, session.container_id)
    else:
        stopped = session_backend.stop_session(session.user.username, session.pid, None)
    if not stopped:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to stop the user's Jupyter session.",
        )

    session.pid = None
    session.container_id = None
    session.ssh_port = None
    session.ssh_password = None
    session.status = models.SessionStatus.stopped
    db.add(session)
    usage.close_open_records(db, session.user, reason="admin", commit=False)
    audit.record(
        db, "session.force_stop", actor=admin.username, target=session.user.username,
        ip_address=audit.client_ip(request), commit=False,
    )
    db.commit()
    return {"message": f"Jupyter session for user_id={user_id} stopped."}


# ---------------------------------------------------------------------------
# Platform-wide resource view
# ---------------------------------------------------------------------------

@router.get("/resources")
def platform_resources(db: Session = Depends(get_db)):
    """Everything an operator needs on one screen: host, GPUs, per-user usage."""
    from services import gpu_monitor, limit_audit

    snapshot = metrics.latest()
    # What the kernel is holding, against what Docker was told to apply.  Read
    # back from the cgroup rather than from `docker inspect`, because the two
    # can disagree and the daemon's own record cannot show it.
    divergences = limit_audit.audit_all()
    users = (
        db.query(models.User)
        .filter(models.User.deleted_at.is_(None))
        .order_by(models.User.username)
        .all()
    )

    per_user = []
    for user in users:
        live = metrics.for_user(user.username)
        # One measurement, not two.  This row used to read disk usage from the
        # cached scan of JUPYTER_DATA_DIR while the quota beside it was
        # measured at the workspace's real location, so a user mapped to a
        # home directory showed "0 MB" in red against a quota they were
        # genuinely over.
        budget = quota.snapshot(db, user)
        per_user.append({
            "username": user.username,
            "is_active": user.is_active,
            "gpu_indices": (
                [int(i) for i in user.gpu_assignment.gpu_indices.split(",") if i.strip().isdigit()]
                if user.gpu_assignment else []
            ),
            "session_status": (
                user.jupyter_session.status.value if user.jupyter_session else "none"
            ),
            "cpu_percent": (live or {}).get("cpu_percent"),
            "memory": (live or {}).get("memory") or {},
            "disk_used_mb": budget["disk"]["used_mb"],
            "quota": budget,
            "enforced": container_manager.effective_limits(user.username),
            # Settings Docker accepted that the kernel is not actually holding.
            # Absent for every workspace that agrees, which is the normal case.
            "limit_divergences": divergences.get(user.username) or [],
        })

    return {
        "collected_at": snapshot.get("collected_at"),
        "host": snapshot.get("host", {}),
        "gpus": gpu_monitor.get_gpu_status(detailed=True),
        # Whether those figures can be believed.  Without this the dashboard
        # cannot tell "no GPUs on this machine" from "the backend lost sight
        # of them", and the two look identical exactly when it matters.
        "gpu_telemetry": gpu_monitor.telemetry(),
        "users": per_user,
        "locked_accounts": ratelimit.status(),
        # The same findings again, flat, so the dashboard can show one banner
        # without walking every user to discover whether anything is wrong.
        "limit_divergences": [
            dict(finding, username=name)
            for name, findings in sorted(divergences.items())
            for finding in findings
        ],
        "settings": {
            "session_backend": session_backend.active_backend(),
            "idle_timeout_minutes": settings.IDLE_TIMEOUT_MINUTES,
            "default_memory_limit_mb": settings.DEFAULT_MEMORY_LIMIT_MB,
            "default_cpu_cores": settings.DEFAULT_CPU_CORES,
            "allow_mock_gpu": settings.ALLOW_MOCK_GPU,
            "disk_quota_action": settings.DISK_QUOTA_ACTION,
            "quota_period": quota.period_kind(),
            "quota_period_label": quota.period_label(),
            "quota_period_resets": quota.period_reset_text(),
            "job_time_quota_action": settings.JOB_TIME_QUOTA_ACTION,
            "cgroup_v2": container_manager.cgroup_v2(),
            "io_limit_device": container_manager.detect_data_disk_device(),
        },
    }


# ---------------------------------------------------------------------------
# Usage accounting & audit trail
# ---------------------------------------------------------------------------

@router.get("/jobs")
def all_jobs(
    active_only: bool = Query(False),
    finished_only: bool = Query(False),
    status: Optional[str] = Query(None, description="one status, or several comma-separated"),
    q: Optional[str] = Query(None, max_length=128, description="match user, name or script"),
    sort: str = Query("id"),
    order: str = Query("desc"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=200),
    capacity: bool = Query(True, description="include the GPU, CPU and fair-share figures"),
    db: Session = Depends(get_db),
):
    """Every user's jobs, a page at a time, plus what the queue is waiting on.

    An administrator sees the whole machine, so this list is as long as the
    platform has been busy and is filtered and paged the same way a user's own
    history is.  The counts are taken over everything rather than over the
    page, because "3 running" must not become "0 running" when the reader
    turns to page two.

    *capacity* exists because the screen shows two of these lists side by
    side: the second asks with it off, and the GPU, CPU and fair-share work is
    done once per refresh rather than twice.
    """
    from services import fairshare, gpu_monitor, jobs as job_service

    query = db.query(models.Job)
    if active_only:
        query = query.filter(models.Job.status.in_(job_service.ACTIVE_STATUSES))
    elif finished_only:
        query = query.filter(models.Job.status.notin_(job_service.ACTIVE_STATUSES))
    query = job_service.filter_jobs(query, status=status, search=q, with_user=True)
    query = job_service.sort_jobs(query, sort, order)
    rows, pagination = job_service.paginate(query, page, per_page)

    # One ordering computed per request, not per row.
    order_map = job_service.queued_order(db)
    payload = {
        "jobs": [job_service.view(db, job, order=order_map) for job in rows],
        "pagination": pagination,
        "counts": job_service.status_counts(db),
    }
    if capacity:
        payload.update({
            "gpus": job_service.gpu_availability(db),
            "cpu": job_service.cpu_capacity(db),
            "fair_share": fairshare.explain(db),
            "gpu_telemetry": gpu_monitor.telemetry(),
        })
    return payload


@router.post("/jobs/cancel", response_model=MessageResponse)
def admin_cancel_jobs(
    payload: dict,
    request: Request,
    db: Session = Depends(get_db),
    admin: models.User = Depends(require_admin),
):
    """Cancel any user's jobs, for reclaiming a GPU that is being hogged."""
    from services import jobs as job_service

    ids = payload.get("ids") or []
    result = job_service.cancel(db, admin, [int(i) for i in ids], by_admin=True)
    audit.record(
        db, "job.admin_cancel", actor=admin.username,
        detail=f"jobs={result['cancelled']}", ip_address=audit.client_ip(request),
    )
    return {"message": f"Cancelled {len(result['cancelled'])} job(s)."}


@router.get("/usage")
def usage_report(
    days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
):
    """GPU-hours per user over the last *days*, plus the raw session list."""
    since = datetime.utcnow() - timedelta(days=days)

    rows = (
        db.query(
            models.UsageRecord.username,
            func.sum(models.UsageRecord.gpu_seconds).label("gpu_seconds"),
            func.sum(models.UsageRecord.cpu_seconds).label("cpu_seconds"),
            func.count(models.UsageRecord.id).label("sessions"),
            func.max(models.UsageRecord.peak_memory_mb).label("peak_memory_mb"),
        )
        .filter(models.UsageRecord.started_at >= since)
        .group_by(models.UsageRecord.username)
        .order_by(func.sum(models.UsageRecord.gpu_seconds).desc())
        .all()
    )

    # Jobs submitted in the period, per user, the other half of "who used what".
    job_rows = (
        db.query(
            models.Job.username,
            func.count(models.Job.id).label("submitted"),
            func.sum(
                case((models.Job.status == models.JobStatus.succeeded, 1), else_=0)
            ).label("succeeded"),
            func.sum(
                case((models.Job.status == models.JobStatus.failed, 1), else_=0)
            ).label("failed"),
        )
        .filter(models.Job.created_at >= since)
        .group_by(models.Job.username)
        .all()
    )
    jobs_by_user = {r.username: r for r in job_rows}

    recent = (
        db.query(models.UsageRecord)
        .filter(models.UsageRecord.started_at >= since)
        .order_by(models.UsageRecord.started_at.desc())
        .limit(200)
        .all()
    )

    return {
        "since": since.isoformat(),
        "days": days,
        "totals": [
            {
                "username": row.username,
                "gpu_hours": round((row.gpu_seconds or 0) / 3600.0, 2),
                "cpu_hours": round((row.cpu_seconds or 0) / 3600.0, 2),
                "sessions": row.sessions,
                "peak_memory_mb": row.peak_memory_mb,
                "jobs_submitted": getattr(jobs_by_user.get(row.username), "submitted", 0),
                "jobs_succeeded": getattr(jobs_by_user.get(row.username), "succeeded", 0),
                "jobs_failed": getattr(jobs_by_user.get(row.username), "failed", 0),
            }
            for row in rows
        ],
        # Why the queue is in the order it is in.
        "fair_share": __import__(
            "services.fairshare", fromlist=["explain"]
        ).explain(db),
        "sessions": [
            {
                "username": r.username,
                "gpu_indices": r.gpu_indices,
                "gpu_count": r.gpu_count,
                "image": r.image,
                "backend": r.backend,
                "started_at": r.started_at.isoformat(),
                "ended_at": r.ended_at.isoformat() if r.ended_at else None,
                "gpu_hours": round((r.gpu_seconds or 0) / 3600.0, 2),
                "cpu_hours": round((r.cpu_seconds or 0) / 3600.0, 2),
                "peak_memory_mb": r.peak_memory_mb,
                "end_reason": r.end_reason,
            }
            for r in recent
        ],
    }


@router.get("/audit")
def audit_log(
    limit: int = Query(100, ge=1, le=1000),
    action: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Most recent audit entries, newest first."""
    query = db.query(models.AuditLog)
    if action:
        query = query.filter(models.AuditLog.action == action)
    rows = query.order_by(models.AuditLog.created_at.desc()).limit(limit).all()
    return [
        {
            "created_at": r.created_at.isoformat(),
            "actor": r.actor,
            "action": r.action,
            "target": r.target,
            "detail": r.detail,
            "ip_address": r.ip_address,
        }
        for r in rows
    ]
