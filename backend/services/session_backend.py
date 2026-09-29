"""Jupyter session backends.

Two interchangeable backends implement the same session-level API so routers
stay backend-agnostic:

* ``process``  raw ``jupyter lab`` subprocess on the host (default; no
  Docker daemon required).  Isolation: OS user + rlimits + GPU ACLs.
* ``container``, one Docker container per user (recommended for production
  with GPUs).  Isolation: GPU device requests + cgroup memory/CPU caps +
  network namespacing.

Select via ``SESSION_BACKEND`` in ``.env`` (``process`` | ``container``).
"""

import logging
from typing import Any, Dict, Optional

from config import settings

logger = logging.getLogger(__name__)


def _backend() -> str:
    return (settings.SESSION_BACKEND or "process").lower()


def active_backend() -> str:
    """Name of the currently configured session backend."""
    return _backend()


# ---------------------------------------------------------------------------
# Session-level API (dispatch by SESSION_BACKEND)
# ---------------------------------------------------------------------------

def start_session(
    username: str,
    gpu_indices: str,
    port: int,
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
    backend: Optional[str] = None,
    image: Optional[str] = None,
    reserved_ssh_ports: Optional[list] = None,
    user=None,
    max_processes: Optional[int] = None,
) -> Dict[str, Any]:
    """Start the user's JupyterLab.  Returns handle info for the DB row.

    Process backend → ``{"pid": ..., "port": ...}``
    Container backend → ``{"container_id": ..., "name": ..., "port": <internal>,
    "ssh_port": ..., "ssh_password": ...}``

    ``backend`` pins the implementation for this one call (used by the
    start endpoint, which must store how the session was actually launched).
    """
    chosen = (backend or _backend()).lower()
    if chosen == "container":
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
            max_processes=max_processes,
        )
        # Inside the container Jupyter always listens on CONTAINER_PORT; the
        # proxy reaches it by Docker-network DNS, so the platform port is
        # informational only.  Store the container-internal port.
        result["port"] = container_manager.CONTAINER_PORT
        return result

    # ── process backend ──────────────────────────────────────────────────
    from services import jupyter_manager

    pid = jupyter_manager.start_jupyter(
        username=username,
        gpu_indices=gpu_indices,
        port=port,
        token=token,
        memory_limit_mb=memory_limit_mb,
        cpu_limit_seconds=cpu_limit_seconds,
        jupyter_password=jupyter_password,
    )
    if pid is None:
        return {"pid": None, "port": port}
    return {"pid": pid, "port": port}


def is_alive(username: str, pid: Optional[int], container_id: Optional[str]) -> bool:
    """Is the user's JupyterLab currently alive?

    Dispatch is driven by the session's own handle (container_id wins over
    pid) rather than the configured SESSION_BACKEND, so probes stay correct
    even when the setting changed after the session was started.
    """
    if container_id:  # session was launched as a container
        from services import container_manager

        state = container_manager.get_container_state(username)
        return bool(state and state.get("running"))

    from services import jupyter_manager

    return pid is not None and jupyter_manager.is_process_alive(pid)


def get_state(username: str) -> Optional[Dict[str, Any]]:
    """Raw backend state for the user's session (container info or None)."""
    if _backend() != "container":
        return None

    from services import container_manager

    return container_manager.get_container_state(username)


def wait_for_ready(username: str, timeout: int = 60) -> bool:
    """Block until the user's Jupyter answers HTTP (container backend only).

    Called from *sync* endpoint code so the shared event loop is never
    blocked.  Returns False on timeout or when the container dies.
    """
    if _backend() != "container":
        return True  # process backend is ready as soon as the PID exists
    from services import container_manager

    return container_manager.wait_until_running(
        container_manager.container_name(username), timeout=timeout
    )


def get_logs(username: str, tail: int = 40) -> str:
    """Recent container logs for the user's session ('' when unavailable)."""
    if _backend() != "container":
        return ""
    from services import container_manager

    return container_manager.get_container_logs(
        container_manager.container_name(username), tail=tail
    )


def stop_session(username: str, pid: Optional[int], container_id: Optional[str]) -> bool:
    """Stop the user's JupyterLab (idempotent)."""
    if container_id:  # session was launched as a container
        from services import container_manager

        return container_manager.stop_user_container(username)

    from services import jupyter_manager

    if pid is not None:
        return jupyter_manager.stop_jupyter(pid)
    return True


def target_base_url(username: str, port: int, backend: Optional[str] = None) -> str:
    """Where the proxy should forward requests for this user.

    ``backend`` pins the implementation (sessions remember how they were
    launched via their ``container_id``).

    Process backend  → ``http://127.0.0.1:<port>``
    Container backend → ``http://gpu-jupyter-<username>:8888`` (Docker DNS)
    """
    chosen = (backend or _backend()).lower()
    if chosen == "container":
        from services import container_manager

        return f"http://{container_manager.container_hostname(username)}:{container_manager.CONTAINER_PORT}"
    return f"http://127.0.0.1:{port}"


def self_heal(sessions: list) -> list:
    """Restart any container marked running in the DB but dead in Docker."""
    if _backend() != "container":
        return []
    from services import container_manager

    return container_manager.ensure_containers_healthy(sessions)


def reconcile(sessions: list) -> list:
    """Re-attach the DB to reality after a backend restart.

    The backend used to stop every user session on shutdown, so a platform
    update destroyed everyone's running notebooks.  Sessions now survive; this
    pass runs at startup and simply corrects rows whose process or container
    did not survive (host reboot, manual docker rm).

    Returns a list of ``{"user": ..., "action": ...}`` corrections.
    """
    import models

    corrections = []
    for session in sessions:
        username = session.user.username
        alive = False
        try:
            alive = is_alive(username, session.pid, session.container_id)
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
