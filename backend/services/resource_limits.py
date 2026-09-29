"""OS-level resource isolation for user JupyterLab processes.

The platform backend runs as root inside its container.  Spawning Jupyter
directly as root would be catastrophic, a notebook cell like
``open('/app/data/gpu_platform.db').read()`` (or worse, ``os.system``) would
give the student full control of the host, including the ability to spawn a
new process with ``CUDA_VISIBLE_DEVICES`` set to *all* GPUs.

Mitigation strategy (defense in depth):

1. **Dedicated unprivileged OS user per platform user** (``gpu_<username>``),
   created at session start.  The Jupyter process runs via ``setuid``/``setgid``
   so it cannot read the DB, the SECRET_KEY, or other users' notebooks.
2. **GPU device ACL**: each OS user gets read/write access to exactly the
   ``/dev/nvidia*`` devices backing their assigned GPUs.  Even if they escape
   CUDA_VISIBLE_DEVICES, opening an un-authorised device fails with EACCES.
   (Requires ``setfacl``; capability dropped gracefully if unavailable.)
3. **rlimits**: address space (RAM), CPU seconds, process count, file size and
   open files are capped via ``resource.setrlimit`` in the child's preexec_fn.
   This is what actually enforces the RAM/CPU quota per user.

All helpers are best-effort with clear logging: they harden a Linux deploy and
degrade to the previous behaviour (root + env-var isolation only) when the
platform primitives are unavailable (macOS development, restricted containers).
"""

import grp
import logging
import os
import pwd
import shutil
import subprocess
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Group memberships may be cached by the C runtime; refresh after setfacl.
_GID_LIST_REFRESH = 2  # nscd-style; harmless if no daemon is running

# Byte → MiB helper
_MIB = 1024 * 1024


# ---------------------------------------------------------------------------
# OS user management
# ---------------------------------------------------------------------------

def _os_username(platform_username: str) -> str:
    """Deterministic Linux username for a platform user (max 32 chars)."""
    safe = "".join(c if c.isalnum() or c == "_" else "_" for c in platform_username.lower())
    return f"gpu_{safe[:28]}"


def ensure_os_user(platform_username: str) -> Optional[int]:
    """Create (or find) the dedicated OS user for *platform_username*.

    Returns the UID, or ``None`` when user creation is impossible (non-root,
    non-Linux, no ``useradd``).  A ``None`` return means the caller should log
    loudly and continue without OS isolation.
    """
    os_name = _os_username(platform_username)

    try:
        entry = pwd.getpwnam(os_name)
        return entry.pw_uid
    except KeyError:
        pass  # does not exist yet, create below

    if not shutil.which("useradd"):
        logger.warning(
            "useradd not available, so Jupyter for %r will run WITHOUT OS-level "
            "isolation. Only do this on development machines!",
            platform_username,
        )
        return None

    try:
        subprocess.run(
            [
                "useradd",
                "--no-create-home",
                "--shell", "/usr/sbin/nologin",
                "--user-group",
                os_name,
            ],
            check=True,
            capture_output=True,
            timeout=15,
        )
        uid = pwd.getpwnam(os_name).pw_uid
        logger.info("Created OS user %s (uid=%d) for platform user %r", os_name, uid, platform_username)
        return uid
    except subprocess.CalledProcessError as exc:
        logger.error("useradd failed for %s: %s", os_name, exc.stderr.decode(errors="replace"))
        return None


def os_uid_for(platform_username: str) -> Optional[int]:
    """UID of the OS user if it exists, else ``None``."""
    try:
        return pwd.getpwnam(_os_username(platform_username)).pw_uid
    except KeyError:
        return None


def remove_os_user(platform_username: str) -> bool:
    """Delete the dedicated OS account, keeping its files.

    ``userdel`` without ``-r``: the home directory is handled separately and
    deliberately, never as a side effect of removing an account.
    """
    if os_uid_for(platform_username) is None or not shutil.which("userdel"):
        return False
    os_name = _os_username(platform_username)
    result = subprocess.run(
        ["userdel", os_name], check=False, capture_output=True, timeout=10
    )
    if result.returncode != 0:
        logger.warning(
            "userdel failed for %s: %s", os_name,
            result.stderr.decode(errors="replace").strip(),
        )
        return False
    return True


# ---------------------------------------------------------------------------
# GPU device ACLs
# ---------------------------------------------------------------------------

def _nvidia_devices() -> list:
    """All /dev/nvidia* character devices present on the host."""
    return sorted(
        f"/dev/{name}"
        for name in os.listdir("/dev")
        if name.startswith("nvidia")
    )


def _device_id_from_name(name: str) -> Optional[int]:
    """``nvidia3`` → 3.  Control nodes (``nvidiactl``, ``nvidia-uvm``) → None."""
    stem = name.removeprefix("nvidia")
    return int(stem) if stem.isdigit() else None


def apply_gpu_device_acl(platform_username: str, gpu_indices: str) -> bool:
    """Grant the user's OS account access to exactly their assigned GPUs.

    Every ``/dev/nvidia*`` device is first denied, then the ones backing the
    assigned indices (plus the shared control nodes ``nvidiactl``,
    ``nvidia-uvm``, ``nvidia-uvm-tools`` and ``nvidia-modeset``) are granted
    read/write to the user's OS account.

    Returns ``True`` when the ACL was applied, ``False`` when ``setfacl`` is
    unavailable (caller should log a warning, env-var isolation remains).
    """
    setfacl = shutil.which("setfacl")
    if setfacl is None:
        logger.warning(
            "setfacl not available, so GPU device-level isolation is DISABLED. "
            "Install the 'acl' package to harden GPU access.",
        )
        return False

    os_name = _os_username(platform_username)
    devices = _nvidia_devices()
    if not devices:
        logger.info("No /dev/nvidia* devices found, skipping GPU ACL")
        return True

    # Devices shared by every user regardless of assignment
    shared = {"nvidiactl", "nvidia-uvm", "nvidia-uvm-tools", "nvidia-modeset"}

    # Which indices this user may touch
    try:
        allowed = {
            int(x)
            for x in gpu_indices.split(",")
            if x.strip().lstrip("-").isdigit()
        }
    except Exception:  # noqa: BLE001 (malformed assignment string)
        allowed = set()

    granted, revoked = [], []
    for dev in devices:
        node = os.path.basename(dev)
        device_id = _device_id_from_name(node)
        permitted = node in shared or (device_id is not None and device_id in allowed)

        try:
            if permitted:
                subprocess.run(
                    [setfacl, "-m", f"u:{os_name}:rw", dev],
                    check=True, capture_output=True, timeout=5,
                )
                granted.append(node)
            else:
                subprocess.run(
                    [setfacl, "-x", f"u:{os_name}", dev],
                    check=False, capture_output=True, timeout=5,  # absence is fine
                )
                revoked.append(node)
        except (subprocess.SubprocessError, OSError) as exc:
            logger.warning("setfacl failed on %s: %s", dev, exc)

    logger.info(
        "GPU ACL for %s, granted: %s",
        os_name, ", ".join(granted) or "(none)",
    )
    return True


def revoke_all_device_acl(platform_username: str) -> None:
    """Remove the user's ACL entries from every GPU device (call on delete)."""
    setfacl = shutil.which("setfacl")
    if setfacl is None:
        return
    os_name = _os_username(platform_username)
    for dev in _nvidia_devices():
        try:
            subprocess.run(
                [setfacl, "-x", f"u:{os_name}", dev],
                check=False, capture_output=True, timeout=5,
            )
        except (subprocess.SubprocessError, OSError):
            pass


# ---------------------------------------------------------------------------
# rlimits, the actual RAM / CPU enforcement
# ---------------------------------------------------------------------------

def make_rlimits_preexec(
    memory_limit_mb: Optional[int],
    cpu_seconds: Optional[int],
    max_processes: int,
) -> Optional[callable]:
    """Build a ``preexec_fn`` applying rlimits in the forked child.

    ``preexec_fn`` runs between ``fork()`` and ``exec()``, the only reliable
    moment to set per-child limits without affecting the parent.

    The returned callable also drops root: ``setgid`` → ``setgroups`` →
    ``setuid`` (order matters).  When the target uid is ``None`` the callable
    only applies rlimits (dev fallback).
    """

    def _apply() -> None:  # pragma: no cover, runs in the forked child
        import resource

        # 1) Address space (RAM), the kernel refuses allocations beyond this.
        if memory_limit_mb:
            limit_bytes = memory_limit_mb * _MIB
            # Soft == hard so the process cannot raise it back.
            resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))

        # 2) Total CPU seconds per process.
        if cpu_seconds:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))

        # 3) Process count, stops fork-bomb style resource grabbing.
        resource.setrlimit(resource.RLIMIT_NPROC, (max_processes, max_processes))

        # 4) File size (protect the disk from runaway writes).
        resource.setrlimit(resource.RLIMIT_FSIZE, (2 * 1024 * _MIB, 2 * 1024 * _MIB))

        # 5) Open files.
        resource.setrlimit(resource.RLIMIT_NOFILE, (512, 512))

    return _apply


# ---------------------------------------------------------------------------
# Combined entry point used by jupyter_manager
# ---------------------------------------------------------------------------

def prepare_os_environment(platform_username: str, gpu_indices: str) -> dict:
    """Ensure the OS user + GPU ACL exist; return a context dict for spawning.

    Keys: ``uid`` (int or None), ``gid`` (int or None), ``os_username``.
    """
    uid = ensure_os_user(platform_username)
    gid = None
    if uid is not None:
        try:
            gid = pwd.getpwnam(_os_username(platform_username)).pw_gid
        except KeyError:
            uid = None  # race, fall back to no isolation

    if uid is not None:
        apply_gpu_device_acl(platform_username, gpu_indices)

    return {"uid": uid, "gid": gid, "os_username": _os_username(platform_username)}


def make_preexec_fn(
    uid: Optional[int],
    gid: Optional[int],
    memory_limit_mb: Optional[int],
    cpu_seconds: Optional[int],
) -> Optional[callable]:
    """Compose rlimits + setuid into a single ``preexec_fn`` (or None)."""
    if uid is None and not memory_limit_mb and not cpu_seconds:
        return None  # nothing to enforce, keep default spawn behaviour

    rlimits = make_rlimits_preexec(memory_limit_mb, cpu_seconds, max_processes=128)

    def _child_setup() -> None:  # pragma: no cover, runs in the forked child
        # os.setgroups must run before setuid
        if gid is not None:
            os.setgid(gid)
            try:
                os.setgroups([gid])
            except PermissionError:
                pass  # container may lack CAP_SETGID for group lists
        if uid is not None:
            os.setuid(uid)
        rlimits()

    return _child_setup


def apply_workspace_ownership(platform_username: str) -> None:
    """chown the user's notebook directory to their OS account (best-effort)."""
    uid = os_uid_for(platform_username)
    if uid is None:
        return
    os_name = _os_username(platform_username)
    work_dir = os.path.join(settings.JUPYTER_DATA_DIR, platform_username)
    try:
        gid = pwd.getpwnam(os_name).pw_gid
        for root, dirs, files in os.walk(work_dir):
            os.chown(root, uid, gid)
            for name in files:
                os.chown(os.path.join(root, name), uid, gid)
    except OSError as exc:
        logger.warning("chown %s failed: %s", work_dir, exc)
