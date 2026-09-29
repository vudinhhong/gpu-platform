"""Deleting a user, reversibly.

Deletion used to be immediate and partial: the row vanished, and with it the
usage and job history that the statistics are built from, while the files, the
containers and any running job stayed behind.  A workspace directory left that
way is unreachable because it belongs to a container uid, so the administrator cannot
even remove it, and the freed username hands those files to whoever registers
it next.

So a delete now moves the account to a trash:

* **Everything stops.**  The workspace container, every job (queued or running)
  and every container the user owns are stopped and removed, open usage records
  are closed, tokens are revoked and the job credential is destroyed.
* **The row stays**, marked with ``deleted_at``.  The username and email remain
  taken, so a new account cannot be created with that name and inherit the
  files, and the history stays attached to its owner.
* **The workspace is renamed**, not deleted: ``<user>`` becomes
  ``.deleted-<user>-<timestamp>`` next to it.  A directory that is somebody's
  real home is never renamed, only the platform's own sidecar beside it is.
* **A restore puts it back.**  The archived directory is renamed to its old
  name if it is still there; if it is not, the user simply starts with a fresh
  workspace.

Only emptying the trash is destructive, and only an administrator doing it
deliberately can reach that.
"""

import json
import logging
import os
import re
import shutil
from datetime import datetime
from typing import Any, Dict, List, Optional

from config import settings

logger = logging.getLogger(__name__)

ARCHIVE_PREFIX = ".deleted-"

# ``.deleted-<username>-<YYYYMMDD-HHMMSS>``.  Matched strictly, and matched
# again before anything is removed: this name is the only thing standing
# between an admin click and ``rm -rf`` on a path built from it.
_ARCHIVE_RE = re.compile(
    r"^\.deleted-(?P<username>[A-Za-z0-9._-]{1,64})-"
    r"(?P<stamp>\d{8}-\d{6})$"
)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _data_root() -> str:
    """The per-user data root as THIS process sees it."""
    return settings.JUPYTER_DATA_DIR


def _resolve_within_root(name: str) -> str:
    """Absolute path of *name* inside the data root, or raise.

    Guards the two ways a name could escape: a separator or ``..`` in the name
    itself, and a symlink planted in the data root that points elsewhere.
    """
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise ValueError(f"invalid directory name: {name!r}")
    root = os.path.realpath(_data_root())
    target = os.path.realpath(os.path.join(root, name))
    if target != root and not target.startswith(root + os.sep):
        raise ValueError(f"{name!r} resolves outside the data root")
    return target


def archive_name(username: str, when: Optional[datetime] = None) -> str:
    """Name to rename *username*'s workspace to when it is trashed."""
    stamp = (when or datetime.utcnow()).strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", username)[:64]
    return f"{ARCHIVE_PREFIX}{safe}-{stamp}"


def parse_archive(name: str) -> Optional[Dict[str, Any]]:
    """``{"username", "deleted_at"}`` for an archive name, else None."""
    match = _ARCHIVE_RE.match(name)
    if not match:
        return None
    try:
        when = datetime.strptime(match.group("stamp"), "%Y%m%d-%H%M%S")
    except ValueError:
        return None
    return {"username": match.group("username"), "deleted_at": when}


# ---------------------------------------------------------------------------
# Archiving the workspace directory
# ---------------------------------------------------------------------------

def archive_workspace(user) -> Optional[str]:
    """Rename the user's platform directory aside.  Returns the new name.

    For a user mapped to a directory that already existed on this machine, that
    directory is theirs and is left exactly as it is; what gets renamed is only
    the platform's own sidecar (the job token, the limits file, the host keys),
    which the platform created and which means nothing without the account.
    """
    live = os.path.join(_data_root(), user.username)
    if not os.path.isdir(live):
        return None

    name = archive_name(user.username)
    destination = os.path.join(_data_root(), name)
    # A second delete of the same user inside one second would collide.
    suffix = 1
    while os.path.exists(destination):
        suffix += 1
        name = f"{archive_name(user.username)}-{suffix}"
        destination = os.path.join(_data_root(), name)

    try:
        os.rename(live, destination)
    except OSError as exc:
        logger.error("Could not archive workspace for %r: %s", user.username, exc)
        return None
    logger.info("Archived workspace %s -> %s", user.username, name)
    return name


def restore_workspace(user) -> str:
    """Put an archived workspace back.  Returns what happened.

    ``restored``  the archive was renamed back to the user's name
    ``fresh``     nothing to restore; a new workspace is created on first start
    ``kept``      a live directory already exists, so the archive was left alone
    """
    live = os.path.join(_data_root(), user.username)
    name = user.archived_workspace
    if not name:
        return "fresh"

    try:
        source = _resolve_within_root(name)
    except ValueError as exc:
        logger.error("Refusing to restore %r: %s", name, exc)
        return "fresh"

    if not os.path.isdir(source):
        return "fresh"
    if os.path.exists(live):
        # Something already occupies the name, never overwrite it.  The
        # archive stays in the trash for the admin to deal with by hand.
        logger.warning(
            "Not restoring %s: %s already exists", name, live
        )
        return "kept"

    try:
        os.rename(source, live)
    except OSError as exc:
        logger.error("Could not restore workspace %r: %s", name, exc)
        return "fresh"
    logger.info("Restored workspace %s -> %s", name, user.username)
    return "restored"


def remove_directory(name: str) -> bool:
    """Permanently delete one directory inside the data root."""
    try:
        target = _resolve_within_root(name)
    except ValueError as exc:
        logger.error("Refusing to delete %r: %s", name, exc)
        return False
    if not os.path.isdir(target):
        return False
    try:
        shutil.rmtree(target)
    except OSError as exc:
        logger.error("Could not delete %r: %s", name, exc)
        return False
    logger.info("Deleted directory %s", name)
    return True


# ---------------------------------------------------------------------------
# Stopping everything the user has running
# ---------------------------------------------------------------------------

def stop_everything(db, user) -> Dict[str, Any]:
    """Stop and remove every container and job belonging to *user*."""
    import models
    from services import container_manager, jobs as job_service, resource_limits
    from services import session_backend, usage

    result = {"jobs_cancelled": [], "containers_removed": 0, "session_stopped": False}

    # Jobs first: cancelling marks them cancelled and stops their containers,
    # which keeps the ledger honest.  Doing it after the container sweep would
    # leave rows saying "running" for containers that no longer exist.
    active = (
        db.query(models.Job)
        .filter(
            models.Job.user_id == user.id,
            models.Job.status.in_([models.JobStatus.queued, models.JobStatus.running]),
        )
        .all()
    )
    if active:
        outcome = job_service.cancel(db, user, [j.id for j in active], by_admin=True)
        result["jobs_cancelled"] = outcome["cancelled"]

    session = user.jupyter_session
    if session is not None:
        try:
            session_backend.stop_session(user.username, session.pid, session.container_id)
            result["session_stopped"] = True
        except Exception as exc:  # noqa: BLE001 (deletion must not be blockable)
            logger.warning("Could not stop session for %r: %s", user.username, exc)
        db.delete(session)

    # Sweep: anything still labelled with this user, running or exited.  Job
    # containers are only stopped when a job ends, not removed, so without this
    # they would pile up for an account that no longer exists.
    result["containers_removed"] = container_manager.remove_user_containers(user.username)

    usage.close_open_records(db, user, reason="deleted", commit=False)

    try:
        resource_limits.revoke_all_device_acl(user.username)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not revoke device ACLs for %r: %s", user.username, exc)

    return result


# ---------------------------------------------------------------------------
# The three transitions
# ---------------------------------------------------------------------------

def _snapshot(db, user) -> Optional[str]:
    """JSON of state held in other tables, so a restore can replay it."""
    assignment = user.gpu_assignment
    if assignment is None:
        return None
    return json.dumps({
        "gpu_assignment": {
            "gpu_indices": assignment.gpu_indices,
            "memory_limit_mb": assignment.memory_limit_mb,
            "cpu_cores": assignment.cpu_cores,
            "cpu_limit_seconds": assignment.cpu_limit_seconds,
            "max_processes": assignment.max_processes,
        }
    })


def soft_delete(db, user, actor: str, ip_address: Optional[str] = None) -> Dict[str, Any]:
    """Move a user to the trash."""
    import models
    from auth import revoke_tokens
    from services import audit, workspaces

    stopped = stop_everything(db, user)

    user.restore_state = _snapshot(db, user)
    if user.gpu_assignment is not None:
        # Removed rather than kept, so every view that counts assigned GPUs
        # stops counting this one without needing to know about the trash.
        db.delete(user.gpu_assignment)

    space = workspaces.for_user(user)
    user.archived_workspace = archive_workspace(user)

    user.is_active = False
    user.deleted_at = datetime.utcnow()
    user.job_token_hash = None   # the submit/queue/cancel commands stop working
    revoke_tokens(user)          # every issued token dies immediately
    db.add(user)

    detail = (
        f"archived={user.archived_workspace or 'none'} "
        f"jobs_cancelled={len(stopped['jobs_cancelled'])} "
        f"containers_removed={stopped['containers_removed']} "
        f"home_kept={'yes' if space.mapped else 'n/a'}"
    )
    audit.record(
        db, "user.trash", actor=actor, target=user.username,
        detail=detail, ip_address=ip_address, commit=False,
    )
    db.commit()
    return {
        "archived_workspace": user.archived_workspace,
        "mapped_home_kept": space.host_path if space.mapped else None,
        **stopped,
    }


def restore(db, user, actor: str, ip_address: Optional[str] = None) -> Dict[str, Any]:
    """Bring a user back out of the trash."""
    import models
    from services import audit

    outcome = restore_workspace(user)

    state = {}
    if user.restore_state:
        try:
            state = json.loads(user.restore_state)
        except ValueError:
            state = {}
    assignment_state = state.get("gpu_assignment")
    if assignment_state and user.gpu_assignment is None:
        db.add(models.GpuAssignment(user_id=user.id, **assignment_state))

    user.deleted_at = None
    user.is_active = True
    user.archived_workspace = None
    user.restore_state = None
    db.add(user)

    audit.record(
        db, "user.restore", actor=actor, target=user.username,
        detail=f"workspace={outcome}", ip_address=ip_address, commit=False,
    )
    db.commit()
    return {"workspace": outcome, "gpu_assignment_restored": bool(assignment_state)}


def purge(db, user, actor: str, ip_address: Optional[str] = None) -> Dict[str, Any]:
    """Delete a trashed user for good, with their archived workspace."""
    from services import audit, workspaces

    from services import resource_limits

    space = workspaces.for_user(user)
    removed = remove_directory(user.archived_workspace) if user.archived_workspace else False
    username = user.username

    # The dedicated OS account outlives a trashed user so a restore keeps the
    # same uid; once the account is gone for good, so should it be.
    try:
        resource_limits.remove_os_user(username)
    except Exception as exc:  # noqa: BLE001 (never block the delete)
        logger.warning("Could not remove the OS account for %r: %s", username, exc)

    audit.record(
        db, "user.purge", actor=actor, target=username,
        detail=(
            f"archive_removed={removed} "
            f"home_kept={'yes' if space.mapped else 'n/a'}"
        ),
        ip_address=ip_address, commit=False,
    )
    db.delete(user)
    db.commit()
    return {"archive_removed": removed, "mapped_home_kept": space.mapped}


# ---------------------------------------------------------------------------
# What the trash view shows
# ---------------------------------------------------------------------------

def _size_mb(name: str) -> int:
    from services import metrics

    try:
        return metrics.directory_size_mb(_resolve_within_root(name))
    except ValueError:
        return 0


def list_trash(db) -> Dict[str, Any]:
    """Trashed users, plus directories on disk that no account owns.

    The second list matters as much as the first: before the trash existed,
    every deleted user left their workspace behind under its original name.
    Those directories are invisible to every other view and cannot be removed
    without root, so they are surfaced here to be cleaned up.
    """
    import models

    users = (
        db.query(models.User)
        .filter(models.User.deleted_at.isnot(None))
        .order_by(models.User.deleted_at.desc())
        .all()
    )

    entries = []
    claimed = set()
    for user in users:
        name = user.archived_workspace
        if name:
            claimed.add(name)
        entries.append({
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "full_name": user.full_name,
            "deleted_at": user.deleted_at,
            "archived_workspace": name,
            "archive_exists": bool(name and os.path.isdir(os.path.join(_data_root(), name))),
            "size_mb": _size_mb(name) if name else 0,
            "home_path": user.home_path,
            "gpu_assignment_saved": bool(user.restore_state),
        })

    # Anything on disk that is neither a live user's workspace nor an archive
    # we just listed.
    live = {
        u.username
        for u in db.query(models.User).filter(models.User.deleted_at.is_(None)).all()
    }
    orphans = []
    root = _data_root()
    try:
        names = sorted(os.listdir(root))
    except OSError:
        names = []
    for name in names:
        if not os.path.isdir(os.path.join(root, name)):
            continue
        if name in live or name in claimed:
            continue
        parsed = parse_archive(name)
        orphans.append({
            "name": name,
            "username": parsed["username"] if parsed else name,
            "deleted_at": parsed["deleted_at"] if parsed else None,
            "size_mb": _size_mb(name),
            "kind": "archive" if parsed else "left over",
        })

    return {"users": entries, "orphans": orphans}
