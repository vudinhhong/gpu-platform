"""Self-service endpoints for the logged-in user.

* ``GET  /api/user/me``             – profile + GPU assignment
* ``PUT  /api/user/me/profile``     – edit own name / email
* ``GET  /api/user/me/gpu``         – GPUs this user may use
* ``GET  /api/user/me/resources``   – live CPU/RAM/disk + quota standing
* ``GET  /api/user/me/images``      – Jupyter images they may pick from
* ``*    /api/user/me/jupyter/*``   – start / stop / status of their session
"""

import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

import models
from auth import get_current_user
from config import settings
from database import get_db
from schemas import JupyterSessionResponse, ProfileUpdate, UserWithAssignment
from services import (
    audit,
    container_manager,
    crypto,
    jobs as job_service,
    gpu_monitor,
    metrics,
    quota,
    session_backend,
    usage,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/user", tags=["user"])


def _resolve_limits(current_user: models.User):
    """Resolve this user's RAM / CPU caps from assignment → platform defaults.

    Returns ``(memory_limit_mb, cpu_cores, cpu_limit_seconds, max_processes)``.
    Cores is the cgroup cap (``cpu.max``); CPU-seconds is a per-process
    RLIMIT_CPU ceiling the admin form no longer offers and nothing applies any
    more, kept so old assignments still load.  ``max_processes`` is ``pids.max``.
    """
    assignment = current_user.gpu_assignment
    memory_limit_mb = assignment.memory_limit_mb if assignment else None
    cpu_cores = assignment.cpu_cores if assignment else None
    cpu_limit_seconds = getattr(assignment, "cpu_limit_seconds", None) if assignment else None
    max_processes = getattr(assignment, "max_processes", None) if assignment else None

    if not memory_limit_mb and settings.DEFAULT_MEMORY_LIMIT_MB:
        memory_limit_mb = settings.DEFAULT_MEMORY_LIMIT_MB
    if not cpu_cores and settings.DEFAULT_CPU_CORES:
        cpu_cores = settings.DEFAULT_CPU_CORES
    if not cpu_limit_seconds and settings.DEFAULT_CPU_LIMIT_SECONDS:
        cpu_limit_seconds = settings.DEFAULT_CPU_LIMIT_SECONDS
    if not max_processes and settings.CONTAINER_PIDS_LIMIT:
        max_processes = settings.CONTAINER_PIDS_LIMIT

    return memory_limit_mb, cpu_cores, cpu_limit_seconds, max_processes


def _assigned_indices(user: models.User) -> List[int]:
    assignment = user.gpu_assignment
    if assignment is None:
        return []
    return [
        int(x)
        for x in assignment.gpu_indices.split(",")
        if x.strip().lstrip("-").isdigit()
    ]


def _session_payload(user: models.User, session: models.JupyterSession) -> Dict[str, Any]:
    """Owner-facing view of a session: secrets decrypted, runtime attached."""
    payload = JupyterSessionResponse.model_validate(session).model_dump(mode="json")
    payload["token"] = crypto.decrypt(session.token) or ""
    payload["ssh_password"] = crypto.decrypt(session.ssh_password)
    # Deliberately no engine state or raw startup logs here: this payload is
    # rendered on the user's dashboard, and neither is theirs to reason about.
    # Administrators have the audit log and the server log for that.
    payload["runtime"] = {
        "resources": metrics.for_user(user.username),
        # False when a newer environment has been built since this workspace
        # started.  Restarting is the only way to pick it up.
        "environment_current": (
            container_manager.environment_is_current(user.username)
            if session.container_id else None
        ),
    }
    return payload


# ---------------------------------------------------------------------------
# Profile / resources
# ---------------------------------------------------------------------------

@router.get("/me", response_model=UserWithAssignment)
async def read_profile(current_user: models.User = Depends(get_current_user)):
    """Profile of the current user including their GPU assignment (if any)."""
    return current_user


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@router.put("/me/profile", response_model=UserWithAssignment)
def update_profile(
    payload: ProfileUpdate,
    request: Request,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Let a user correct their own name and email address.

    Only the fields the form actually sent are touched, and only these two:
    quotas, the admin flag and the workspace path stay an administrator's to
    set.  The email is unique across the platform, including accounts sitting
    in the trash, because they still own theirs.
    """
    sent = payload.model_fields_set
    changes = []

    if "email" in sent:
        email = (payload.email or "").strip()
        if not _EMAIL_RE.fullmatch(email):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Enter a valid email address.",
            )
        if len(email) > 255:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email address too long (max 255 characters).",
            )
        if email.lower() != (current_user.email or "").lower():
            clash = (
                db.query(models.User)
                .filter(models.User.email == email, models.User.id != current_user.id)
                .first()
            )
            if clash is not None:
                # Whose it is is none of their business, only that it is taken.
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="That email address is already in use.",
                )
            current_user.email = email
            changes.append("email")

    if "full_name" in sent:
        full_name = (payload.full_name or "").strip()
        if len(full_name) > 255:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Name too long (max 255 characters).",
            )
        if full_name != (current_user.full_name or ""):
            current_user.full_name = full_name or None
            changes.append("full_name")

    if changes:
        db.add(current_user)
        audit.record(
            db, "user.profile", actor=current_user.username,
            target=current_user.username, detail=",".join(changes),
            ip_address=audit.client_ip(request), commit=False,
        )
        db.commit()
        db.refresh(current_user)
    return current_user


@router.get("/me/gpu", response_model=List[Dict[str, Any]])
async def read_my_gpus(current_user: models.User = Depends(get_current_user)):
    """Live status of only the GPUs assigned to this user (admins see all)."""
    if current_user.is_admin:
        return gpu_monitor.get_gpu_status(detailed=True)
    indices = _assigned_indices(current_user)
    return gpu_monitor.get_gpu_status_for(indices) if indices else []


@router.get("/me/resources")
def read_my_resources(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Live CPU/RAM/disk consumption of this user's session plus quota standing.

    The platform claimed to manage RAM and CPU but never showed either; this is
    the data behind the dashboard's resource card.
    """
    return {
        "container": metrics.for_user(current_user.username),
        "quota": quota.snapshot(db, current_user),
        "limits": dict(zip(
            ("memory_limit_mb", "cpu_cores", "cpu_limit_seconds", "max_processes"),
            _resolve_limits(current_user),
        )),
        # What the Docker daemon actually applied, read back from it, a
        # setting it rejects is dropped so the session can still start, and
        # that must not look like an enforced limit.
        "enforced": container_manager.effective_limits(current_user.username),
        "collected_at": metrics.latest().get("collected_at"),
    }


@router.get("/me/usage")
def read_my_usage(
    days: int = 30,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """This user's own consumption: GPU hours, CPU hours and job counts."""
    from datetime import timedelta

    from sqlalchemy import func

    days = max(1, min(365, int(days)))
    since = datetime.utcnow() - timedelta(days=days)

    totals = (
        db.query(
            func.coalesce(func.sum(models.UsageRecord.gpu_seconds), 0.0),
            func.coalesce(func.sum(models.UsageRecord.cpu_seconds), 0.0),
            func.count(models.UsageRecord.id),
        )
        .filter(
            models.UsageRecord.user_id == current_user.id,
            models.UsageRecord.started_at >= since,
        )
        .first()
    )
    jobs_total = (
        db.query(models.Job.status, func.count(models.Job.id))
        .filter(models.Job.user_id == current_user.id, models.Job.created_at >= since)
        .group_by(models.Job.status)
        .all()
    )

    return {
        "days": days,
        "gpu_hours": round((totals[0] or 0.0) / 3600.0, 2),
        "cpu_hours": round((totals[1] or 0.0) / 3600.0, 2),
        "runs": totals[2] or 0,
        "jobs": {status.value: count for status, count in jobs_total},
        "quota": quota.snapshot(db, current_user),
    }


@router.get("/me/images")
def read_available_images(current_user: models.User = Depends(get_current_user)):
    """Jupyter images this user may launch, and the one they last picked."""
    return {
        "images": container_manager.available_images(),
        "selected": current_user.preferred_image or settings.JUPYTER_IMAGE,
        "supported": True,
    }


# ---------------------------------------------------------------------------
# Jupyter session management
# ---------------------------------------------------------------------------

def _assignment_to_cuda_string(assignment: models.GpuAssignment) -> str:
    indices = [
        x.strip()
        for x in assignment.gpu_indices.split(",")
        if x.strip().lstrip("-").isdigit()
    ]
    return ",".join(indices) if indices else ""


@router.get("/me/jupyter/status")
def jupyter_status(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the user's Jupyter session, reconciling DB state with reality.

    Sync def.  Docker SDK calls here must not block the shared event loop.
    """
    session = current_user.jupyter_session
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="You have no workspace yet.",
        )

    # Reconcile: process/container died without us noticing (OOM, reboot…)
    if session.status == models.SessionStatus.running:
        alive = session_backend.is_alive(
            current_user.username, session.container_id
        )
        if not alive:
            session.status = models.SessionStatus.stopped
            session.pid = None
            session.container_id = None
            db.add(session)
            usage.close_open_records(db, current_user, reason="died", commit=False)
            db.commit()
            db.refresh(session)

    # Keep the accounting ledger's peak-RAM figure current.
    live = metrics.for_user(current_user.username)
    if live and (live.get("memory") or {}).get("used_mb"):
        usage.note_peak_memory(db, current_user.id, live["memory"]["used_mb"])

    return _session_payload(current_user, session)


@router.post("/me/jupyter/start")
def jupyter_start(
    request: Request,
    payload: Optional[Dict[str, Any]] = None,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Start (or report) the user's personal JupyterLab server.

    Sync def on purpose: creating the user container and waiting for its
    Jupyter endpoint must never block the shared asyncio event loop.
    """
    session = current_user.jupyter_session

    # Already running and healthy → return as-is
    if (
        session is not None
        and session.status == models.SessionStatus.running
        and session_backend.is_alive(
            current_user.username, session.container_id
        )
    ):
        return _session_payload(current_user, session)

    # Nothing here refuses a workspace.  The hour budgets belong to the job
    # queue, not to somebody's own seat at the machine, and being over the disk
    # budget costs the GPU rather than the workspace, since the workspace is
    # the only place the user can delete files from; see below.
    budget = quota.workspace_state(db, current_user)

    # Jupyter listens on the same port inside every container; the proxy finds
    # it by Docker-network DNS, so no host port is allocated or consumed here.
    port = container_manager.CONTAINER_PORT
    token = session_backend.generate_token()

    if session is None:
        session = models.JupyterSession(
            user_id=current_user.id,
            port=port,
            token=crypto.encrypt(token),
            base_url=f"/jupyter/{current_user.username}/",
            status=models.SessionStatus.starting,
        )
        db.add(session)
        db.flush()  # assign session.id
    else:
        session.port = port
        session.token = crypto.encrypt(token)
        session.base_url = f"/jupyter/{current_user.username}/"
        session.status = models.SessionStatus.starting
        # The row is reused, but this is a new session: anything that asks how
        # long it has been running, the quota grace period, for one, needs
        # this to mean "started at", not "first ever started at".
        session.created_at = datetime.utcnow()

    gpu_string = ""
    if current_user.gpu_assignment is not None:
        gpu_string = _assignment_to_cuda_string(current_user.gpu_assignment)

    # Over the disk budget: start, but without a GPU.  Cleaning up needs a
    # file manager and a terminal, not a graphics card.
    over_disk = bool(budget["disk"]["over"])
    if over_disk and gpu_string:
        logger.info(
            "Starting %r without a GPU: %s MB used of %s MB",
            current_user.username, budget["disk"]["used_mb"], budget["disk"]["quota_mb"],
        )
        gpu_string = ""

    memory_limit_mb, cpu_cores, cpu_limit_seconds, max_processes = _resolve_limits(current_user)

    requested_image = (payload or {}).get("image") or current_user.preferred_image

    # No image, no workspace.  This used to fall back to a second backend that
    # ran Jupyter as a subprocess beside every other user's, which turned a
    # missing image into a silent loss of isolation; it is gone.  ./deploy.sh
    # builds the image, so say what is wrong and let an administrator fix it.
    chosen_image = container_manager.resolve_image(requested_image)
    if not container_manager.image_exists(chosen_image):
        logger.error(
            "Workspace image %s is missing, refusing to start a workspace for %r. "
            "Run ./deploy.sh to build it.",
            chosen_image, current_user.username,
        )
        session.status = models.SessionStatus.error
        db.add(session)
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The workspace image is not available on this platform yet. "
                   "Please contact an administrator.",
        )

    # A port is assigned on a user's first start and stays theirs, so every
    # port already assigned to somebody else is off the table whether or not
    # their workspace happens to be running right now.  Only deactivating or
    # trashing an account returns its port to the pool.
    reserved_ports = [
        row[0]
        for row in db.query(models.JupyterSession.ssh_port)
        .filter(
            models.JupyterSession.ssh_port.isnot(None),
            models.JupyterSession.user_id != current_user.id,
        )
        .all()
    ]

    base_url = f"/jupyter/{current_user.username}/"
    try:
        handle = session_backend.start_session(
            username=current_user.username,
            gpu_indices=gpu_string,
            token=token,
            base_url=base_url,
            memory_limit_mb=memory_limit_mb,
            cpu_cores=cpu_cores,
            cpu_limit_seconds=cpu_limit_seconds,
            max_processes=max_processes,
            ssh_public_key=current_user.ssh_public_key,
            # A user-chosen Jupyter password wins and stays a second factor;
            # otherwise the one derived from their account password is used and
            # the proxy keeps waving them through.
            jupyter_password=(
                current_user.hashed_jupyter_password
                or (current_user.account_jupyter_hash if settings.UNIFIED_PASSWORD else None)
            ),
            jupyter_password_required=bool(current_user.hashed_jupyter_password),
            unix_password_hash=(
                current_user.unix_password_hash if settings.UNIFIED_PASSWORD else None
            ),
            disk_quota_mb=quota.disk_quota_mb(current_user),
            image=chosen_image,
            reserved_ssh_ports=reserved_ports,
            preferred_ssh_port=session.ssh_port,
            user=current_user,
        )
    except Exception as exc:  # noqa: BLE001
        session.status = models.SessionStatus.error
        db.add(session)
        db.commit()
        db.refresh(session)
        # The raw error names images, daemons and sockets, infrastructure the
        # user did not ask about and cannot act on.  It goes to the log for an
        # administrator; they get something they can actually do something with.
        logger.error("Starting workspace for %r failed: %s", current_user.username, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not start your workspace. Please try again. If it keeps "
                   "failing, contact an administrator.",
        )

    if not handle.get("container_id"):
        session.status = models.SessionStatus.error
        db.add(session)
        db.commit()
        db.refresh(session)
        logger.error("Workspace for %r produced no handle", current_user.username)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not start your workspace. Please try again, if it keeps "
                   "failing, contact an administrator.",
        )

    session.container_id = handle.get("container_id")
    session.port = handle.get("port", port)
    session.image = handle.get("image")
    session.ssh_port = handle.get("ssh_port")
    session.ssh_password = crypto.encrypt(handle.get("ssh_password"))

    # Wait until Jupyter actually answers so the UI can truthfully say
    # "Running".  This blocks only this worker thread.
    ready = container_manager.wait_until_running(
        container_manager.container_name(current_user.username),
        base_url=session.base_url,
        token=token,
    )
    if not ready:
        # Keep the startup output for whoever has to diagnose it, but do
        # not hand a wall of infrastructure logs to the person who just
        # wanted to open a notebook.
        logger.error(
            "Workspace for %r did not become ready. Startup output:\n%s",
            current_user.username,
            container_manager.get_container_logs(
                container_manager.container_name(current_user.username), tail=30
            ) or "(none)",
        )
        session.status = models.SessionStatus.error
        db.add(session)
        db.commit()
        db.refresh(session)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Your workspace did not finish starting in time. Please try "
                   "again. If it keeps failing, contact an administrator.",
        )

    session.status = models.SessionStatus.running
    session.last_activity = datetime.utcnow()
    db.add(session)

    # Refresh the credential the in-workspace job commands use.  Rotating it
    # per session means a workspace that is gone cannot queue work any more.
    if settings.JOBS_ENABLED:
        job_service.ensure_job_token(db, current_user)

    usage.open_record(
        db, current_user, gpu_indices=gpu_string,
        image=session.image, backend="container", cpu_cores=cpu_cores or 0.0,
    )
    audit.record(
        db, "session.start", actor=current_user.username,
        target=current_user.username,
        detail=f"gpus={gpu_string or 'none'} image={session.image}",
        ip_address=audit.client_ip(request), commit=False,
    )
    db.commit()
    db.refresh(session)

    payload_out = _session_payload(current_user, session)
    if over_disk:
        # Say it here rather than leaving them to notice the GPU is missing.
        payload_out["notice"] = quota.over_disk_message(budget)
    return payload_out


@router.post("/me/jupyter/stop")
def jupyter_stop(
    request: Request,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Stop the user's JupyterLab server (no-op if not running)."""
    session = current_user.jupyter_session
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="You have no workspace to stop.",
        )

    live = metrics.for_user(current_user.username) or {}
    peak = (live.get("memory") or {}).get("used_mb")

    session_backend.stop_session(current_user.username, session.container_id)

    session.pid = None
    session.container_id = None
    # ssh_port is deliberately kept.  It is the port this workspace asks for
    # next time, and an SSH client keys known_hosts by host *and* port: handing
    # somebody a different port on the next start makes their client report that
    # the host key changed, which reads as an attack and not as a restart.
    # Nothing mistakes a remembered port for a live one -- every reader checks
    # the session status first.
    session.ssh_password = None
    session.status = models.SessionStatus.stopped
    session.last_activity = datetime.utcnow()
    db.add(session)
    usage.close_open_records(db, current_user, reason="user", peak_memory_mb=peak, commit=False)
    audit.record(
        db, "session.stop", actor=current_user.username, target=current_user.username,
        ip_address=audit.client_ip(request), commit=False,
    )
    db.commit()
    db.refresh(session)
    return _session_payload(current_user, session)


# ---------------------------------------------------------------------------
# SSH / credentials
# ---------------------------------------------------------------------------

@router.get("/me/ssh")
def ssh_info(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """SSH connection info for the user's running container (owner only)."""
    if not settings.SSH_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="SSH access is not available on this platform.",
        )

    session = current_user.jupyter_session
    if session is None or session.status != models.SessionStatus.running or not session.ssh_port:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Start your workspace first; SSH becomes available with it.",
        )

    # With unified passwords there is no separate SSH secret to reveal: the
    # container accepts the user's own account password.  Only a pre-upgrade
    # account with no derived credential still gets a per-session password.
    session_password = crypto.decrypt(session.ssh_password)
    uses_account_password = bool(
        settings.UNIFIED_PASSWORD and current_user.unix_password_hash and not session_password
    )
    return {
        "enabled": True,
        "host": None,  # the UI fills in window.location.hostname
        "port": session.ssh_port,
        "username": current_user.username,
        "uses_account_password": uses_account_password,
        "password_auth_enabled": settings.SSH_PASSWORD_AUTH,
        "password": None if uses_account_password else session_password,
        "public_key_set": bool(current_user.ssh_public_key),
    }


@router.put("/me/jupyter-password")
def set_jupyter_password(
    payload: dict,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Set or clear a Jupyter-specific password.

    Optional.  By default Jupyter accepts the user's platform account password
    (derived automatically) and the proxy signs them in, so nothing needs
    configuring.  Setting a password here makes it a real second factor: the
    proxy stops vouching and Jupyter's own form must be satisfied.  The
    plaintext never touches the database or the container, only the argon2
    hash.  Takes effect on the next session start.
    """
    import re as _re

    from auth import hash_jupyter_password

    new_password = (payload.get("password") or "").strip() if isinstance(payload, dict) else ""
    if new_password:
        if len(new_password) < 8:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Jupyter password must be at least 8 characters.",
            )
        if len(new_password) > 128:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Jupyter password too long (max 128 characters).",
            )
        if not _re.fullmatch(r"[\x21-\x7e]+", new_password):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Use printable ASCII characters without spaces.",
            )
        current_user.hashed_jupyter_password = hash_jupyter_password(new_password)
        message = "Jupyter password set. It applies from your next session start."
    else:
        current_user.hashed_jupyter_password = None
        message = "Jupyter password cleared. Your account password is used instead."

    db.add(current_user)
    audit.record(
        db, "user.jupyter_password", actor=current_user.username,
        detail="set" if new_password else "cleared", commit=False,
    )
    db.commit()
    return {"message": message}


@router.put("/me/ssh-key")
def set_ssh_key(
    payload: dict,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Register/clear the caller's SSH public key (OpenSSH one-line format)."""
    key = (payload.get("public_key") or "").strip() if isinstance(payload, dict) else ""
    if key:
        parts = key.split()
        if len(parts) < 2 or parts[0] not in {
            "ssh-rsa", "ssh-ed25519", "ecdsa-sha2-nistp256",
            "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
        }:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Provide a valid OpenSSH public key, e.g. 'ssh-ed25519 AAAA... comment'.",
            )
        if len(key) > 1024:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Key too long.",
            )

    current_user.ssh_public_key = key or None
    db.add(current_user)
    audit.record(
        db, "user.ssh_key", actor=current_user.username,
        detail="set" if key else "cleared", commit=False,
    )
    db.commit()

    # Apply to the live session too.  The container only reads SSH_PUBLIC_KEY
    # from its environment at creation time, so without this a key added after
    # starting a session did nothing until the next restart, the user just
    # kept getting a password prompt.
    applied_live = container_manager.install_authorized_keys(
        current_user.username, key or None
    )

    if key:
        message = (
            "SSH public key saved and active now."
            if applied_live
            else "SSH public key saved. It applies from your next session start."
        )
    else:
        message = "SSH public key cleared."
    return {"message": message, "applied_live": applied_live}


@router.put("/me/image")
def set_preferred_image(
    payload: dict,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Pick the Jupyter image for the next session start (allow-list enforced)."""
    requested = (payload.get("image") or "").strip() if isinstance(payload, dict) else ""
    resolved = container_manager.resolve_image(requested) if requested else None
    if requested and resolved != requested:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That image is not offered by this platform.",
        )

    current_user.preferred_image = resolved
    db.add(current_user)
    db.commit()
    return {
        "message": (
            f"Image set to {resolved}. It applies on your next session start."
            if resolved else "Image reset to the platform default."
        ),
        "selected": resolved or settings.JUPYTER_IMAGE,
    }
