"""JupyterLab process management service.

Responsibilities
----------------
* Find a free TCP port in the configured range.
* Spawn a ``jupyter lab`` subprocess with the correct environment
  (``CUDA_VISIBLE_DEVICES``, ``ServerApp.token``, ``ServerApp.base_url``).
* Check whether a tracked PID is still alive.
* Gracefully (SIGTERM then SIGKILL) stop a Jupyter process.
* Generate cryptographically secure session tokens.
"""

import logging
import os
import secrets
import signal
import socket
import subprocess
import time
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)

# Grace period (seconds) between SIGTERM and SIGKILL when stopping a process
_STOP_GRACE_PERIOD = 8


# ---------------------------------------------------------------------------
# Token generation
# ---------------------------------------------------------------------------

def generate_token() -> str:
    """Return a 64-character hex token suitable for Jupyter ``ServerApp.token``."""
    return secrets.token_hex(32)  # 32 bytes → 64 hex chars


# ---------------------------------------------------------------------------
# Port management
# ---------------------------------------------------------------------------

def find_available_port() -> Optional[int]:
    """Scan the configured port range and return the first unbound port.

    Returns ``None`` if every port in the range is already in use.
    """
    for port in range(settings.JUPYTER_PORT_START, settings.JUPYTER_PORT_END + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    logger.error(
        "No available port in range %d-%d",
        settings.JUPYTER_PORT_START,
        settings.JUPYTER_PORT_END,
    )
    return None


# ---------------------------------------------------------------------------
# Process lifecycle
# ---------------------------------------------------------------------------

def is_process_alive(pid: int) -> bool:
    """Return True if a process with *pid* exists and is running.

    Uses ``os.kill(pid, 0)`` which sends no signal but raises ``OSError``
    when the process does not exist or the caller has no permission.
    """
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but belongs to another user; treat as alive.
        return True


def start_jupyter(
    username: str,
    gpu_indices: str,
    port: int,
    token: str,
    memory_limit_mb: int = None,
    cpu_limit_seconds: int = None,
    jupyter_password: str = None,
) -> Optional[int]:
    """Spawn a JupyterLab process for *username*.

    Args:
        username:    Platform username – used for the working directory and
                     the ``base_url`` path segment.
        gpu_indices: Comma-separated GPU indices to expose, e.g. ``"0,1"``.
        port:        TCP port the Jupyter server should bind to.
        token:       Authentication token for ``ServerApp.token``.

    Returns:
        The PID of the spawned process, or ``None`` on failure.
    """
    # Ensure per-user working directory exists
    work_dir = os.path.join(settings.JUPYTER_DATA_DIR, username)
    os.makedirs(work_dir, exist_ok=True)

    # Log directory – one file per user, append mode
    log_dir = os.path.join(settings.JUPYTER_DATA_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"{username}.log")

    base_url = f"/jupyter/{username}/"

    # Password mode (preferred): pass the *hashed* password through traitlets
    # config so logins are verified by Jupyter itself and the browser keeps a
    # session cookie, no secret in any URL.  Fall back to token mode.
    auth_args = [f"--ServerApp.token={token}"]
    if jupyter_password:
        cfg_dir = os.path.join(work_dir, ".jupyter")
        os.makedirs(cfg_dir, exist_ok=True)
        with open(os.path.join(cfg_dir, "jupyter_server_config.json"), "w") as fh:
            import json as _json

            _json.dump(
                {
                    "IdentityProvider": {"hashed_password": jupyter_password},
                    "ServerApp": {"token": "", "password_required": True},
                },
                fh,
            )
        auth_args = ["--ServerApp.token=", "--ServerApp.password_required=True"]

    cmd = [
        "jupyter", "lab",
        "--ip=127.0.0.1",
        f"--port={port}",
        "--no-browser",
        *auth_args,
        f"--ServerApp.base_url={base_url}",
        "--ServerApp.allow_origin=*",
        "--ServerApp.allow_remote_access=True",
        # Disable the default redirect from '/' to '/lab'
        "--ServerApp.open_browser=False",
    ]

    # Inherit the host environment, then override GPU visibility.
    # NOTE: this is only *one* layer of isolation, see resource_limits.py for
    # the OS-level ACL that makes GPU escape impossible rather than discouraged.
    env = os.environ.copy()
    env["CUDA_VISIBLE_DEVICES"] = gpu_indices
    env["HOME"] = work_dir

    # OS-user isolation + rlimits (RAM / CPU / processes).
    # Best-effort: without root these are skipped (dev machines, macOS).
    from services import resource_limits

    ctx = resource_limits.prepare_os_environment(username, gpu_indices)
    resource_limits.apply_workspace_ownership(username)
    preexec = resource_limits.make_preexec_fn(
        uid=ctx["uid"],
        gid=ctx["gid"],
        memory_limit_mb=memory_limit_mb,
        cpu_seconds=cpu_limit_seconds,
    )

    try:
        log_fh = open(log_path, "a")  # noqa: WPS515  (intentionally kept open)
        process = subprocess.Popen(
            cmd,
            env=env,
            cwd=work_dir,
            stdout=log_fh,
            stderr=log_fh,
            # Detach from the current process group so the child survives
            # if the parent (uvicorn) is restarted.
            start_new_session=True,
            # setuid to the per-user OS account + apply rlimits in the child.
            preexec_fn=preexec,
        )
        logger.info(
            "Started JupyterLab for %r (as %s): PID=%d port=%d gpus=%r mem=%sMB cpu=%ss",
            username, ctx["os_username"], process.pid, port, gpu_indices,
            memory_limit_mb, cpu_limit_seconds,
        )
        return process.pid

    except FileNotFoundError:
        logger.error(
            "Could not start Jupyter for %r – 'jupyter' not found in PATH. "
            "Install JupyterLab inside the container.",
            username,
        )
        return None
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error starting Jupyter for %r: %s", username, exc)
        return None


def stop_jupyter(pid: int) -> bool:
    """Stop the JupyterLab process with *pid*.

    Sends SIGTERM and waits up to ``_STOP_GRACE_PERIOD`` seconds for a clean
    exit, then escalates to SIGKILL.

    Returns:
        ``True`` if the process is no longer alive after the call.
    """
    if not is_process_alive(pid):
        logger.debug("stop_jupyter: PID %d is already dead", pid)
        return True

    logger.info("Sending SIGTERM to Jupyter PID %d", pid)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True  # Gone between the check and the kill – fine
    except PermissionError as exc:
        logger.error("Cannot send SIGTERM to PID %d: %s", pid, exc)
        return False

    # Wait for graceful shutdown
    deadline = time.monotonic() + _STOP_GRACE_PERIOD
    while time.monotonic() < deadline:
        time.sleep(0.5)
        if not is_process_alive(pid):
            logger.info("PID %d exited cleanly", pid)
            return True

    # Force kill
    logger.warning("PID %d did not exit in %ds – sending SIGKILL", pid, _STOP_GRACE_PERIOD)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True  # Race condition – already exited
    except PermissionError as exc:
        logger.error("Cannot send SIGKILL to PID %d: %s", pid, exc)
        return False

    time.sleep(0.5)
    alive = is_process_alive(pid)
    if alive:
        logger.error("PID %d still alive after SIGKILL", pid)
    return not alive
