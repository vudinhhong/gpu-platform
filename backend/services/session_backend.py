"""User sessions: one Docker container per user.

This module is the seam the routers talk to, so nothing outside it has to know
how a workspace is actually started, probed or stopped.  Everything here
delegates to :mod:`services.container_manager`.

There is no subprocess backend that runs ``jupyter lab`` inside the
platform's own container and isolates users with an OS account,
``setrlimit`` and GPU device ACLs.  Such a backend cannot keep users apart:

* Every workspace shares one filesystem.  ``JUPYTER_DATA_DIR/<user>`` is
  created 0755, so each user can read every other user's notebooks and data,
  their Jupyter log, and the ``.jupyter`` config holding the argon2 hash of
  their account password.
* Every workspace shares one PID and one network namespace, so ``ps`` shows
  other users' command lines and every other user's Jupyter is one TCP
  connection away.
* The memory "limit" is ``RLIMIT_AS``, an address-space approximation applied
  per process, not ``memory.max`` applied to the workspace.  There is no disk
  quota, no ``cpu.max``, no SSH, and no image choice.

Closing those holes means giving each user their own mount, PID and network
namespace and moving the limits into cgroups, which is a description of a
container runtime.  Docker is already installed, so the platform uses it.
"""

import logging
import secrets
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Which node a workspace is on
# ---------------------------------------------------------------------------
# A session row with node_id NULL is on the master and everything below goes
# straight to container_manager, as it did before there were nodes.  On
# another node the same functions run there (services.node_ops), reached
# through services.nodes.call.

_FROM_ROW = object()


def _remote_node(username: str, node_id: Any = _FROM_ROW):
    """The node *username*'s workspace is on, or None when it is the master.

    *node_id* given (None included) is taken as is; otherwise it is read from
    the user's session row.
    """
    import models
    from database import SessionLocal

    db = SessionLocal()
    try:
        if node_id is _FROM_ROW:
            row = (db.query(models.JupyterSession.node_id)
                   .join(models.User, models.User.id == models.JupyterSession.user_id)
                   .filter(models.User.username == username).first())
            node_id = row[0] if row else None
        if node_id is None:
            return None
        node = db.query(models.Node).filter(models.Node.id == node_id).first()
        if node is None or node.is_local:
            return None
        db.expunge(node)
        return node
    finally:
        db.close()


def _call(node, op: str, timeout: Optional[float] = None, **args) -> Any:
    from database import SessionLocal
    from services import nodes

    db = SessionLocal()
    try:
        return nodes.call(db, node.id, op, timeout=timeout, **args)
    finally:
        db.close()


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
    node_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Start the user's JupyterLab container.  Returns info for the DB row:
    ``{"container_id", "name", "image", "port", "ssh_port", "ssh_password"}``.
    """
    from services import container_manager

    params = dict(
        username=username,
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
    node = _remote_node(username, node_id)
    if node is not None:
        from services import node_ops

        params["reserved_ssh_ports"] = list(reserved_ssh_ports or [])
        # Pulling an image on a fresh node can take a while.
        return _call(node, "start_workspace", timeout=600, params=params,
                     user=node_ops.user_args(user))
    result = container_manager.start_user_container(**params, user=user)
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

    node = _remote_node(username)
    if node is not None:
        from services.nodes import NodeError

        try:
            state = _call(node, "workspace_state", timeout=20, username=username)
        except NodeError as exc:
            # Unknown is not dead: a node that misses a few heartbeats must not
            # have its workspaces written off.  The proxy answers 502 meanwhile.
            logger.warning("Workspace of %r on %s not checked: %s", username, node.name, exc)
            return True
    else:
        state = container_manager.get_container_state(username)
    return bool(state and state.get("running"))


def get_state(username: str) -> Optional[Dict[str, Any]]:
    """Raw container state for the user's session (None when there is none)."""
    from services import container_manager

    node = _remote_node(username)
    if node is not None:
        return _call(node, "workspace_state", timeout=20, username=username)
    return container_manager.get_container_state(username)


def wait_for_ready(username: str, timeout: Optional[int] = None, base_url: str = "/",
                   token: Optional[str] = None, node_id: Any = _FROM_ROW) -> bool:
    """Block until the user's Jupyter answers HTTP.

    Called from *sync* endpoint code so the shared event loop is never
    blocked.  Returns False on timeout or when the container dies.
    """
    from services import container_manager

    kwargs = {"base_url": base_url, "token": token}
    if timeout is not None:
        kwargs["timeout"] = timeout
    node = _remote_node(username, node_id)
    if node is not None:
        wait = kwargs.get("timeout") or 120
        return bool(_call(node, "workspace_ready", timeout=wait + 30, username=username, **kwargs))
    return container_manager.wait_until_running(container_manager.container_name(username), **kwargs)


def get_logs(username: str, tail: int = 40, node_id: Any = _FROM_ROW) -> str:
    """Recent container logs for the user's session ('' when unavailable)."""
    from services import container_manager

    node = _remote_node(username, node_id)
    if node is not None:
        try:
            return _call(node, "workspace_logs", timeout=30, username=username, tail=tail) or ""
        except Exception:  # noqa: BLE001 (logs are for diagnosis only)
            return ""
    return container_manager.get_container_logs(
        container_manager.container_name(username), tail=tail
    )


def stop_session(username: str, container_id: Optional[str] = None) -> bool:
    """Stop the user's JupyterLab (idempotent).

    ``container_id`` is accepted and ignored: the container is found by name,
    and a row that has none has nothing to stop.
    """
    from services import container_manager

    node = _remote_node(username)
    if node is not None:
        return bool(_call(node, "stop_workspace", timeout=60, username=username))
    return container_manager.stop_user_container(username)


def image_available(image: str, node_id: Optional[int] = None) -> bool:
    from services import container_manager

    if node_id is not None:
        from database import SessionLocal
        from services import nodes

        db = SessionLocal()
        try:
            node = nodes.node_for(db, node_id)
            if not node.is_local:
                return bool(nodes.call(db, node.id, "image_exists", timeout=30, image=image))
        finally:
            db.close()
    return container_manager.image_exists(image)


def target(username: str) -> Tuple[str, Dict[str, str]]:
    """Where the proxy forwards this user's requests, and the headers it adds.

    On the master that is the container by Docker DNS.  On another node it is
    the node's data port, with a header only the master can sign.
    """
    from services import container_manager

    node = _remote_node(username)
    if node is not None:
        from services import nodes

        if not node.data_url:
            raise nodes.NodeError(f"node {node.name!r} has no data URL")
        return f"{node.data_url}/_node", nodes.proxy_header(node, username)
    return (
        f"http://{container_manager.container_hostname(username)}"
        f":{container_manager.CONTAINER_PORT}"
    ), {}


def target_base_url(username: str) -> str:
    return target(username)[0]


def ssh_host(username: str) -> Optional[str]:
    """The host to SSH to when it is not the one the user browses to."""
    from urllib.parse import urlsplit

    node = _remote_node(username)
    if node is None or not node.data_url:
        return None
    return urlsplit(node.data_url).hostname


def self_heal(sessions: list) -> list:
    """Restart any container marked running in the DB but dead in Docker.

    Only the master's own: a node restarts nothing on its own, and a dead
    workspace there is found by the status check and marked stopped.
    """
    from services import container_manager

    local = [s for s in sessions if getattr(s, "node_id", None) is None]
    return container_manager.ensure_containers_healthy(local)


def reconcile(sessions: list) -> list:
    """Re-attach the DB to reality after a backend restart.

    User sessions survive a backend restart, so a platform update does not
    touch running notebooks.  This pass runs at startup and corrects rows whose
    container did not survive (host reboot, manual docker rm).

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
