"""User sessions: one Docker container per user.

This module is the seam the routers talk to, so nothing outside it has to know
how a workspace is actually started, probed or stopped.  Everything here
delegates to :mod:`services.container_manager`.

There used to be a second backend behind a ``SESSION_BACKEND`` switch, which
ran ``jupyter lab`` as a subprocess inside the platform's own container and
isolated users with an OS account, ``setrlimit`` and GPU device ACLs.  It was
removed rather than repaired, because it could not do the one job a session
backend has, which is keeping users apart:

* Every workspace shared one filesystem.  ``JUPYTER_DATA_DIR/<user>`` is
  created 0755, so each user could read every other user's notebooks and data,
  their Jupyter log, and the ``.jupyter`` config holding the argon2 hash of
  their account password.
* Every workspace shared one PID and one network namespace, so ``ps`` showed
  other users' command lines and every other user's Jupyter was one TCP
  connection away.
* The memory "limit" was ``RLIMIT_AS``, an address-space approximation applied
  per process, not ``memory.max`` applied to the workspace.  There was no disk
  quota, no ``cpu.max``, no SSH, and no image choice.

Closing those holes means giving each user their own mount, PID and network
namespace and moving the limits into cgroups, which is a description of a
container runtime.  Docker is already installed, so the platform uses it.
"""

import logging
import secrets
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Token generation
# ---------------------------------------------------------------------------

def generate_token() -> str:
    """A 64-character hex token for Jupyter's ``ServerApp.token``."""
    return secrets.token_hex(32)  # 32 bytes → 64 hex chars


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------

def start_session(
    username: str,
    gpu_indices: str,
    token: str,
    base_url: str,
    memory_limit_mb: Optional[int] = None,
    cpu_cores: Optional[float] = None,
    cpu_limit_seconds: Optional[int] = None,
    ssh_public_key: Optional[str] = None,
    jupyter_password: Optional[str] = None,
    jupyter_password_required: bool = False,
    unix_password_hash: Optional[str] = None,
    disk_quota_mb: Optional[int] = None,
    image: Optional[str] = None,
    reserved_ssh_ports: Optional[list] = None,
    preferred_ssh_port: Optional[int] = None,
    user=None,
    max_processes: Optional[int] = None,
) -> Dict[str, Any]:
    """Start the user's JupyterLab container.  Returns info for the DB row:
    ``{"container_id", "name", "image", "port", "ssh_port", "ssh_password"}``.
    """
    from services import container_manager

    result = container_manager.start_user_container(
        username=username,
        user=user,
        gpu_indices=gpu_indices,
        token=token,
        base_url=base_url,
        memory_limit_mb=memory_limit_mb,
        cpu_cores=cpu_cores,
        cpu_limit_seconds=cpu_limit_seconds,
        ssh_public_key=ssh_public_key,
        jupyter_password=jupyter_password,
        jupyter_password_required=jupyter_password_required,
        unix_password_hash=unix_password_hash,
        disk_quota_mb=disk_quota_mb,
        image=image,
        reserved_ssh_ports=reserved_ssh_ports,
        preferred_ssh_port=preferred_ssh_port,
        max_processes=max_processes,
    )
    # Inside the container Jupyter always listens on CONTAINER_PORT; the proxy
    # reaches it by Docker-network DNS, so the stored port is informational.
    result["port"] = container_manager.CONTAINER_PORT
    return result


def is_alive(username: str, container_id: Optional[str]) -> bool:
    """Is the user's JupyterLab currently alive?

    A row with no ``container_id`` is not running.  That covers a session the
    removed process backend started: the subprocess lived inside the platform's
    own container and did not survive the upgrade, and its PID means nothing
    now.  Reporting it dead is what gets the row corrected on the next probe.
    """
    if not container_id:
        return False

    from services import container_manager

    state = container_manager.get_container_state(username)
    return bool(state and state.get("running"))


def get_state(username: str) -> Optional[Dict[str, Any]]:
    """Raw container state for the user's session (None when there is none)."""
    from services import container_manager

    return container_manager.get_container_state(username)


def wait_for_ready(username: str, timeout: int = 60) -> bool:
    """Block until the user's Jupyter answers HTTP.

    Called from *sync* endpoint code so the shared event loop is never
    blocked.  Returns False on timeout or when the container dies.
    """
    from services import container_manager

    return container_manager.wait_until_running(
        container_manager.container_name(username), timeout=timeout
    )


def get_logs(username: str, tail: int = 40) -> str:
    """Recent container logs for the user's session ('' when unavailable)."""
    from services import container_manager

    return container_manager.get_container_logs(
        container_manager.container_name(username), tail=tail
    )


def stop_session(username: str, container_id: Optional[str] = None) -> bool:
    """Stop the user's JupyterLab (idempotent).

    ``container_id`` is accepted and ignored: the container is found by name,
    and a row that has none has nothing to stop.
    """
    from services import container_manager

    return container_manager.stop_user_container(username)


def target_base_url(username: str) -> str:
    """Where the proxy should forward this user's requests (Docker DNS)."""
    from services import container_manager

    return (
        f"http://{container_manager.container_hostname(username)}"
        f":{container_manager.CONTAINER_PORT}"
    )


def self_heal(sessions: list) -> list:
    """Restart any container marked running in the DB but dead in Docker."""
    from services import container_manager

    return container_manager.ensure_containers_healthy(sessions)


def reconcile(sessions: list) -> list:
    """Re-attach the DB to reality after a backend restart.

    The backend used to stop every user session on shutdown, so a platform
    update destroyed everyone's running notebooks.  Sessions now survive; this
    pass runs at startup and simply corrects rows whose container did not
    survive (host reboot, manual docker rm).

    Returns a list of ``{"user": ..., "action": ...}`` corrections.
    """
    import models

    corrections = []
    for session in sessions:
        username = session.user.username
        alive = False
        try:
            alive = is_alive(username, session.container_id)
        except Exception as exc:  # noqa: BLE001 (docker not reachable yet)
            logger.warning("Reconcile probe failed for %r: %s", username, exc)
            continue
        if session.status == models.SessionStatus.running and not alive:
            session.status = models.SessionStatus.stopped
            session.pid = None
            session.container_id = None
            corrections.append({"user": username, "action": "marked_stopped"})
        elif session.status != models.SessionStatus.running and alive:
            session.status = models.SessionStatus.running
            corrections.append({"user": username, "action": "marked_running"})
    return corrections
