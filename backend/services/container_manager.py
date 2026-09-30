"""Per-user Docker container management.

Each platform user gets a dedicated JupyterLab container rather than a bare
process sharing the platform's own namespaces:

* GPU isolation is enforced by the Docker daemon through **device requests**
  (the API form of ``docker run --gpus device=<uuid>``), a kernel-level
  device cgroup rather than an environment variable.
* RAM / CPU caps use cgroups (``--memory`` / ``--cpus``), which limit the
  whole container (not just one process) and are measured as real RSS.
* The container is attached to the platform's Docker network and addressed by
  DNS name ``gpu-jupyter-<username>``, so no host port is consumed and the
  proxy simply talks to ``http://gpu-jupyter-<username>:8888``.

GPU-detach mitigation lives in three places:
  1. ``scripts/host_gpu_setup.sh``: driver persistence and VRAM preservation
     on the host (root cause).
  2. ``backend/gpu_keeper.sh``: in-container keep-alive touching the driver.
  3. :func:`ensure_containers_healthy`: periodic self-heal.

Requires the Docker SDK (``pip install docker``) and access to
``/var/run/docker.sock``.
"""

import glob
import json
import logging
import os
import re
import secrets
import subprocess
from typing import Any, Dict, Iterable, List, Optional

from config import settings

logger = logging.getLogger(__name__)

# Jupyter listens on this fixed port inside every container
CONTAINER_PORT = 8888

# UID the per-user account runs as inside the container.  The host-side data
# directory is owned by the same numeric id, so files the backend writes there
# are owned by the right user from the container's point of view.
CONTAINER_UID = 1000

# Safe characters for container names
_NAME_RE = re.compile(r"[^a-z0-9_-]")

# Labels let the metrics collector and admin tooling recognise our containers
LABEL_USER = "gpu-platform.user"
LABEL_ROLE = "gpu-platform.role"
LABEL_JOB  = "gpu-platform.job"


def _docker_client():
    """Lazily import and return a Docker SDK client.

    Raises RuntimeError saying what to install when it is unavailable.
    """
    try:
        import docker  # noqa: WPS433 (optional dependency)
    except ImportError as exc:
        raise RuntimeError(
            "The 'docker' Python package is required to run user workspaces. "
            "Install it with:  pip install docker"
        ) from exc

    try:
        client = docker.from_env()
        client.ping()
        return client
    except Exception as exc:  # noqa: BLE001 (daemon down, socket missing…)
        raise RuntimeError(
            "Cannot connect to the Docker daemon (is /var/run/docker.sock "
            "mounted into the backend container and is the user in the "
            "'docker' group?)"
        ) from exc


def container_name(username: str) -> str:
    """Deterministic container name for a platform user."""
    return "gpu-jupyter-" + sanitize_username(username)


def container_hostname(username: str) -> str:
    """DNS name used by the backend proxy to reach the container."""
    return container_name(username)


def sanitize_username(username: str) -> str:
    """Lowercase Linux-safe name.  MUST stay in sync with docker-entrypoint.sh
    (tr 'A-Z' 'a-z' | sed 's/[^a-z0-9_-]/_/g' | cut -c1-28)."""
    return _NAME_RE.sub("_", username.lower())[:28]


# Where a workspace used to be mounted for everyone.  Kept only so a container
# created before the move keeps working until it is next recreated.
LEGACY_WORKSPACE_PATH = "/workspace"


def container_home_path(username: str, workspace=None) -> str:
    """Absolute path of the user's home directory *inside* the container.

    It is both the SSH home and the Jupyter root, two locations meant two
    file trees, and a directory created over SSH landed in a hidden subfolder
    JupyterLab does not display.

    The path mirrors the host: a mapped home keeps its own absolute path, so a
    conda installation inside it still finds itself; anything else gets
    ``/home/<username>``.
    """
    from services import workspaces

    if workspace is not None:
        return workspace.container_path
    return os.path.join(workspaces.CONTAINER_HOME_ROOT, sanitize_username(username))


def host_data_dir() -> str:
    """The per-user data root as seen on the DOCKER HOST.

    Bind mounts passed to the Docker API are resolved on the host, not inside
    the backend container, so user-container volumes must use this path.
    """
    return settings.JUPYTER_DATA_HOST_DIR or settings.JUPYTER_DATA_DIR


# ---------------------------------------------------------------------------
# SSH port allocation
# ---------------------------------------------------------------------------

def generate_ssh_password() -> str:
    """24-char URL-safe password, generated per session start."""
    return secrets.token_urlsafe(18)


def published_host_ports() -> set:
    """Host ports currently published by *any* container on this daemon.

    The previous implementation bound a socket inside the backend container's
    own network namespace, which says nothing about the host; two users could
    be handed the same port and the second session would die at create time.
    Asking the daemon is authoritative for the ports we actually compete for.
    """
    used: set = set()
    try:
        client = _docker_client()
        for container in client.containers.list(all=True):
            ports = (container.attrs.get("NetworkSettings") or {}).get("Ports") or {}
            for bindings in ports.values():
                for binding in bindings or []:
                    try:
                        used.add(int(binding.get("HostPort")))
                    except (TypeError, ValueError):
                        continue
    except Exception as exc:  # noqa: BLE001 (degrade to "nothing known in use")
        logger.warning("Could not enumerate published host ports: %s", exc)
    return used


def ssh_port_candidates(
    reserved: Optional[Iterable[int]] = None,
    preferred: Optional[int] = None,
) -> List[int]:
    """Free host ports in the configured SSH range, best candidate first.

    A workspace keeps the port it was first given.  An SSH client keys
    ``known_hosts`` by host *and* port, so moving a workspace to another port
    makes every client that has ever connected announce that the host key has
    changed -- from the user's seat indistinguishable from a
    machine-in-the-middle, and not clearable by reconnecting.  The host keys are
    already persistent (``docker-entrypoint.sh`` keeps them in a bind mount);
    the port is the other half of that identity.

    *preferred* is the port already assigned to this workspace, and goes first.
    *reserved* is every port assigned to somebody else, whether or not their
    workspace is running: a port belongs to its owner until the account is
    deactivated or moved to the trash, so stopping a workspace never costs it
    its port and never hands it to a neighbour who starts in the meantime.

    The preferred port leads the list rather than being the only candidate: if
    something outside the platform has taken it the workspace still starts, on a
    port that is then recorded as its new one.
    """
    taken = published_host_ports() | {int(p) for p in (reserved or []) if p}
    free = [
        port
        for port in range(settings.SSH_PORT_START, settings.SSH_PORT_END + 1)
        if port not in taken
    ]
    want = int(preferred) if preferred else None
    if want is not None and want in free:
        return [want] + [p for p in free if p != want]
    return free


# ---------------------------------------------------------------------------
# GPU device resolution
# ---------------------------------------------------------------------------

def nvidia_device_nodes(device_ids: Iterable[str]) -> List[str]:
    """The /dev entries a container pinned to these GPUs needs.

    The NVIDIA runtime hook already creates these nodes inside the container,
    so naming them again looks redundant.  It is not, and this is the whole
    point: the hook adds them behind the daemon's back, so Docker never tells
    systemd about them, and the scope's DeviceAllow list ends up without a
    single NVIDIA entry.  The next time systemd reapplies that unit's device
    policy, which a plain `systemctl daemon-reload` is enough to trigger, the
    devices are revoked from a container that is still running: the nodes are
    still in /dev, the host is fine, and every call inside fails with
    "Failed to initialize NVML: Unknown Error".

    Passing them as real devices puts them in the container's OCI spec, where
    runc turns them into DeviceAllow properties, and a reload then reapplies
    them instead of dropping them.  Only the assigned cards are listed, so the
    isolation is exactly what it was.
    """
    from services import gpu_monitor

    controls = ("/dev/nvidiactl", "/dev/nvidia-uvm",
                "/dev/nvidia-uvm-tools", "/dev/nvidia-modeset")
    # What this container was given is the best evidence of what the driver
    # has: the hook injected exactly the control nodes that exist.  With no
    # GPU of its own to look at, ask for the three every driver creates and
    # let the fallback below drop the lot if the daemon disagrees.
    local = {path for path in glob.glob("/dev/nvidia*")}
    nodes: List[str] = [c for c in controls if c in local] if local else list(controls[:3])

    minors = gpu_monitor.device_minor_by_index()
    for raw in device_ids:
        try:
            index = int(raw)
        except (TypeError, ValueError):
            continue
        node = f"/dev/nvidia{minors.get(index, index)}"
        if node not in nodes:
            nodes.append(node)
    return nodes


def _device_kwargs(device_ids: List[str]) -> Dict[str, Any]:
    """Docker kwargs pinning these GPUs, by UUID and by device node."""
    nodes = nvidia_device_nodes(device_ids)
    return {
        "device_requests": _device_requests(device_ids),
        "devices": [f"{n}:{n}:rwm" for n in nodes],
    }


def _device_requests(device_ids: List[str]):
    """Build the Docker DeviceRequest list pinning exactly these GPUs.

    Indices are translated to GPU **UUIDs** when nvidia-smi can resolve them:
    indices are re-ordered by the driver (CUDA_DEVICE_ORDER, hot-plug, MIG),
    whereas a UUID always names the same physical device.
    """
    from docker.types import DeviceRequest

    from services import gpu_monitor

    uuid_by_index = gpu_monitor.uuid_by_index()
    resolved: List[str] = []
    for raw in device_ids:
        try:
            index = int(raw)
        except ValueError:
            continue
        resolved.append(uuid_by_index.get(index) or str(index))

    return [
        DeviceRequest(
            device_ids=resolved,
            capabilities=[["gpu", "compute", "utility"]],
        )
    ]


# ---------------------------------------------------------------------------
# Disk I/O throttling (cgroup blkio / io.max)
# ---------------------------------------------------------------------------

_DISK_DEVICE_CACHE: Optional[str] = None
_CGROUP_V2: Optional[bool] = None


def cgroup_v2() -> bool:
    """True when the host uses the cgroup v2 unified hierarchy."""
    global _CGROUP_V2
    if _CGROUP_V2 is None:
        _CGROUP_V2 = os.path.isfile("/sys/fs/cgroup/cgroup.controllers")
    return _CGROUP_V2


def _parent_disk(device: str) -> str:
    """Resolve a partition to the whole disk backing it.

    I/O throttling is a whole-disk property: writing a partition's device
    number into cgroup v2's ``io.max`` fails with "no such device", and the
    container then starts with no I/O cap at all.  ``/dev/nvme0n1p2`` has to
    become ``/dev/nvme0n1``.
    """
    name = os.path.basename(device)
    sys_block = f"/sys/class/block/{name}"
    if not os.path.exists(os.path.join(sys_block, "partition")):
        return device  # already a whole disk
    try:
        parent = os.path.basename(os.path.dirname(os.path.realpath(sys_block)))
    except OSError:
        return device
    # Validate through sysfs, NOT /dev: this runs inside the backend container,
    # whose /dev holds almost nothing, while the path is resolved by the Docker
    # daemon on the host, where the node does exist.
    if parent and os.path.exists(f"/sys/class/block/{parent}"):
        return f"/dev/{parent}"
    return device


def detect_data_disk_device() -> Optional[str]:
    """Find the block device backing ``JUPYTER_DATA_DIR`` (e.g. ``/dev/nvme0n1``).

    Returns ``None`` when it cannot be determined, in which case no BPS or
    IOPS caps are applied at all.  On cgroup v2 that leaves the container with
    no disk limits whatsoever, since the weight is not sent there either.
    """
    global _DISK_DEVICE_CACHE
    if _DISK_DEVICE_CACHE is not None:
        return _DISK_DEVICE_CACHE or None

    device: Optional[str] = None
    try:
        with open("/proc/self/mountinfo") as fh:
            for line in fh:
                fields = line.split()
                if len(fields) < 5:
                    continue
                major_minor = fields[2]
                mount_point = fields[4]
                if not settings.JUPYTER_DATA_DIR.startswith(mount_point):
                    continue
                if major_minor.startswith("0:"):  # virtual filesystem, skip
                    continue
                dev_node = f"/dev/block/{major_minor}"
                if not os.path.exists(dev_node):
                    continue
                try:
                    real = os.path.realpath(dev_node)
                    if real.startswith("/dev/"):
                        device = real
                        break
                except OSError:
                    continue
    except OSError:
        pass

    if device is None:
        try:
            out = subprocess.run(
                ["df", "-P", settings.JUPYTER_DATA_DIR],
                capture_output=True, text=True, timeout=5,
            ).stdout
            last = out.strip().splitlines()[-1].split()[0] if out else ""
            if last.startswith("/dev/") and not last.startswith("/dev/loop"):
                device = last
        except Exception:  # noqa: BLE001
            pass

    if device:
        resolved = _parent_disk(device)
        if resolved != device:
            logger.info(
                "Data directory sits on partition %s; I/O limits apply to the "
                "whole disk %s (cgroup io.max does not accept partitions)",
                device, resolved,
            )
        device = resolved

    _DISK_DEVICE_CACHE = device or ""
    if device:
        logger.info("User-container disk I/O limits apply to device %s", device)
    else:
        logger.warning(
            "Could not determine the block device for %s, so no disk I/O caps "
            "will be applied to user containers.",
            settings.JUPYTER_DATA_DIR,
        )
    return device


def shm_size_bytes(memory_limit_mb: Optional[int]) -> int:
    """How much /dev/shm a container gets.

    Docker gives 64 MB by default, which breaks anything that passes data
    between processes through shared memory, a PyTorch DataLoader with
    workers being the common case.  Half the memory limit is the ceiling
    because tmpfs pages come out of the same cgroup budget.
    """
    wanted = max(64, int(settings.CONTAINER_SHM_SIZE_MB or 0))
    if memory_limit_mb:
        wanted = min(wanted, max(64, int(memory_limit_mb) // 2))
    return wanted * 1024 * 1024


def _disk_io_limits_kwargs() -> Dict[str, Any]:
    """Compose Docker blkio keyword-args from platform settings."""
    kwargs: Dict[str, Any] = {}

    device = detect_data_disk_device()
    read_bps = settings.DISK_READ_BPS_MB * 1024 * 1024 if settings.DISK_READ_BPS_MB else 0
    write_bps = settings.DISK_WRITE_BPS_MB * 1024 * 1024 if settings.DISK_WRITE_BPS_MB else 0

    if device and read_bps:
        kwargs["device_read_bps"] = [{"Path": device, "Rate": read_bps}]
    if device and write_bps:
        kwargs["device_write_bps"] = [{"Path": device, "Rate": write_bps}]
    if device and settings.DISK_READ_IOPS:
        kwargs["device_read_iops"] = [{"Path": device, "Rate": settings.DISK_READ_IOPS}]
    if device and settings.DISK_WRITE_IOPS:
        kwargs["device_write_iops"] = [{"Path": device, "Rate": settings.DISK_WRITE_IOPS}]

    # blkio weight is a cgroup v1 control.  On the v2 unified hierarchy the
    # daemon does accept it and runc converts it to io.weight, so nothing here
    # fails -- but io.weight is only honoured when the device runs BFQ or has
    # blk-iocost configured, and neither is the default.  Sending it would put
    # a number in `docker inspect` that the kernel then ignores, which is worse
    # than sending nothing.  The BPS ceilings above are what actually bind.
    if settings.BLKIO_WEIGHT and not cgroup_v2():
        kwargs["blkio_weight"] = max(10, min(1000, settings.BLKIO_WEIGHT))

    return kwargs


# ---------------------------------------------------------------------------
# Image
# ---------------------------------------------------------------------------

def image_exists(image_name: str) -> bool:
    """True when the given Jupyter image is already available locally."""
    try:
        _docker_client().images.get(image_name)
        return True
    except Exception:  # noqa: BLE001 (not found or daemon hiccup)
        return False


def available_images() -> List[Dict[str, Any]]:
    """Images users may pick from (``JUPYTER_IMAGES`` = ``label=ref,…``).

    Falls back to the single ``JUPYTER_IMAGE`` when the list is unset.  Each
    entry reports whether it is actually pulled/built on this host so the UI
    can grey out what would fail.
    """
    raw = (settings.JUPYTER_IMAGES or "").strip()
    entries: List[Dict[str, Any]] = []
    if raw:
        for chunk in raw.split(","):
            if "=" in chunk:
                label, ref = chunk.split("=", 1)
            else:
                label, ref = chunk, chunk
            label, ref = label.strip(), ref.strip()
            if ref:
                entries.append({"label": label or ref, "image": ref})
    if not entries:
        entries = [{"label": "Default", "image": settings.JUPYTER_IMAGE}]

    for entry in entries:
        entry["available"] = image_exists(entry["image"])
    return entries


def resolve_image(requested: Optional[str]) -> str:
    """Validate a user-requested image against the allow-list.

    Never trust the client with a raw image reference, because that would let any
    user run an arbitrary image on the host with GPU access.
    """
    allowed = {entry["image"] for entry in available_images()}
    if requested and requested in allowed:
        return requested
    return settings.JUPYTER_IMAGE


def ensure_image(image_name: Optional[str] = None) -> Optional[str]:
    """Return the image name when it exists locally, else None.

    Building here, inside a request handler, would block the backend for the
    whole multi-minute apt/pip install, so the image is baked by ``deploy.sh``
    and merely *verified* here.
    """
    image_name = image_name or settings.JUPYTER_IMAGE
    if image_exists(image_name):
        return image_name
    logger.error(
        "User Jupyter image %r is missing. Build it once before starting "
        "sessions:  docker build -f backend/jupyter.Dockerfile -t %s backend/ "
        "(or run ./deploy.sh)",
        image_name, image_name,
    )
    return None


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def start_user_container(
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
    reserved_ssh_ports: Optional[Iterable[int]] = None,
    preferred_ssh_port: Optional[int] = None,
    user=None,
    max_processes: Optional[int] = None,
) -> Dict[str, Any]:
    """Create and start the user's Jupyter container.

    Returns a dict with container identity plus SSH connection info when the
    SSH sidecar is enabled.
    """
    client = _docker_client()
    chosen_image = ensure_image(resolve_image(image))
    if chosen_image is None:
        raise RuntimeError(
            f"User Jupyter image {settings.JUPYTER_IMAGE!r} not found. "
            "Build it first:  docker build -f backend/jupyter.Dockerfile "
            f"-t {settings.JUPYTER_IMAGE} backend/   (deploy.sh does this "
            "automatically)"
        )
    name = container_name(username)

    # Replace any stale container with the same name
    _remove_existing(client, name)

    device_ids = [x.strip() for x in gpu_indices.split(",") if x.strip()]

    # Bind-mount sources must exist before `docker run`.  Creating them via the
    # backend's own JUPYTER_DATA_DIR view also creates them on the host, and that
    # directory is bind-mounted 1:1 from ./jupyter_data.
    from services import workspaces

    space = workspaces.for_user(user) if user is not None else None
    owner = space.uid if space else CONTAINER_UID
    home_in_container = container_home_path(username, space)

    # The platform's own directory always lives under JUPYTER_DATA_DIR, even
    # when the workspace itself is somebody's existing home.
    os.makedirs(f"{settings.JUPYTER_DATA_DIR}/{username}/.platform", exist_ok=True)
    os.makedirs(f"{settings.JUPYTER_DATA_DIR}/{username}/.ssh_host_keys", exist_ok=True)
    if space is None or not space.mapped:
        os.makedirs(f"{settings.JUPYTER_DATA_DIR}/{username}", exist_ok=True)
        migrate_split_home(username)
    ensure_workspace_ownership(username, space)
    for path in (f"{settings.JUPYTER_DATA_DIR}/{username}/.platform",
                 f"{settings.JUPYTER_DATA_DIR}/{username}/.ssh_host_keys"):
        try:
            os.chown(path, owner, owner)
        except OSError:
            pass
    write_platform_limits_file(username, disk_quota_mb, owner=owner)

    kwargs: Dict[str, Any] = {
        "image": chosen_image,
        "name": name,
        # What the shell prompt shows.  Docker's DNS resolves the container
        # NAME, which is what the proxy uses, so this is free to read like the
        # user's own machine instead of a row in someone's cluster.
        "hostname": "workspace",
        "labels": {LABEL_USER: username, LABEL_ROLE: "jupyter"},
        "environment": {
            "JUPYTER_TOKEN": token,
            "JUPYTER_BASE_URL": base_url,
            "GPU_KEEPER_INTERVAL": str(settings.GPU_KEEPER_INTERVAL),
            # Make pip install default to --user so libraries land in the
            # persistent home volume instead of the throwaway image layer.
            "PIP_USER": "1",
            # Fallback for `platform-limits`; the authoritative, always-current
            # value is the limits file written below.
            "PLATFORM_DISK_QUOTA_MB": str(disk_quota_mb or 0),
            # Every in-container tool reads this rather than assuming a path.
            # Only PLATFORM_HOME: setting HOME here would give root the user's
            # home directory too, and root would leave files in it.
            "PLATFORM_HOME": home_in_container,
        },
        # Persistent storage, surviving container recreation:
        #   <user>/                → /home/<user>.  ONE directory that is both
        #                            the user's SSH home and the Jupyter root, so
        #                            the two views never diverge.  Dotfiles
        #                            (.local for `pip install --user`, .ssh,
        #                            .jupyter, .cache) live inside it and stay
        #                            out of the file browser, which hides them.
        #   <user>/.ssh_host_keys/ → sshd host keys (stable fingerprints)
        "volumes": {
            (space.host_path if space else f"{host_data_dir()}/{username}"): {
                "bind": home_in_container, "mode": "rw",
            },
            # Platform bookkeeping, mounted BESIDE the workspace rather than
            # inside it, so a mapped home stays untouched.
            f"{host_data_dir()}/{username}/.platform": {
                "bind": workspaces.PLATFORM_DIR, "mode": "rw",
            },
            f"{host_data_dir()}/{username}/.ssh_host_keys": {
                "bind": "/etc/ssh/host_keys", "mode": "rw",
            },
        },
        "network": settings.DOCKER_NETWORK,
        "detach": True,
        "auto_remove": False,
        "restart_policy": {"Name": "unless-stopped"},
        # Hardening
        "security_opt": ["no-new-privileges:true"],
        # No raw sockets.  A workspace shares a Docker network with the
        # backend and with every other workspace, and CAP_NET_RAW is what an
        # AF_PACKET capture (tcpdump) and a forged ARP reply both need them; the
        # two ways to read a neighbour's traffic, which here carries job
        # tokens and session tokens over plain HTTP.  Nothing a notebook does
        # needs it; ping is not installed and users cannot install it.
        "cap_drop": ["NET_RAW"],
        # A real init as PID 1.  The entrypoint ends in `exec jupyter lab`, so
        # without this PID 1 is a Python process that never calls wait().  sshd
        # daemonises, and the process serving each login is orphaned onto PID 1
        # when it finishes, one unreaped zombie per SSH/SCP/sftp connection,
        # forever.  A zombie still holds a pids.max slot, so a workspace that
        # stays open long enough runs out: sshd can no longer fork and the
        # client sees "kex_exchange_identification: read: Connection reset by
        # peer" instead of a prompt.  docker-init (tini) reaps orphans, which
        # keeps the count flat no matter how many times someone reconnects.
        "init": True,
        # Fork-bomb ceiling (cgroup pids.max).  Per-user when an administrator
        # set one, since a parallel build or a DataLoader with many workers
        # runs into this long before a fork bomb would.
        "pids_limit": max_processes or settings.CONTAINER_PIDS_LIMIT or None,
        "shm_size": shm_size_bytes(memory_limit_mb or settings.DEFAULT_MEMORY_LIMIT_MB),
    }

    # ── GPU pinning via the Docker daemon (device-cgroup level isolation) ──
    # THIS is the isolation boundary.  CUDA_VISIBLE_DEVICES alone is advisory:
    # on a host whose *default runtime is nvidia* (very common), a container
    # started without device requests can see every GPU and the user only has
    # to unset the variable.
    if device_ids:
        kwargs.update(_device_kwargs(device_ids))
        # Inside the container the assigned GPUs are renumbered from 0, so the
        # user-visible mask is simply "all of them".
        kwargs["environment"]["CUDA_VISIBLE_DEVICES"] = ",".join(
            str(i) for i in range(len(device_ids))
        )
        kwargs["environment"]["NVIDIA_DRIVER_CAPABILITIES"] = "compute,utility"
    else:
        # CPU-only user: 'void' tells the nvidia container runtime to inject
        # nothing at all, even when it is the daemon's default runtime.
        kwargs["environment"]["NVIDIA_VISIBLE_DEVICES"] = "void"
        kwargs["environment"]["CUDA_VISIBLE_DEVICES"] = ""

    # ── SSH sidecar ───────────────────────────────────────────────────────
    ssh_password = None
    candidates: List[int] = []
    if settings.SSH_ENABLED:
        candidates = ssh_port_candidates(reserved_ssh_ports, preferred=preferred_ssh_port)
        if not candidates:
            logger.warning(
                "SSH enabled but no free host port in range %d-%d, starting "
                "container without SSH.",
                settings.SSH_PORT_START, settings.SSH_PORT_END,
            )
        else:
            kwargs["environment"].update({
                "SSH_ENABLED": "1",
                "SSH_USER_NAME": username,
                "SSH_USER_UID": str(owner),
                "SSH_USER_GID": str(space.gid if space else CONTAINER_UID),
                "SSH_PUBLIC_KEY": ssh_public_key or "",
                "SSH_PASSWORD_AUTH": "1" if settings.SSH_PASSWORD_AUTH else "0",
            })
            if unix_password_hash:
                # The user's account password, as a /etc/shadow digest.  The
                # plaintext never leaves the browser, and no second password
                # has to be generated, displayed or remembered.
                kwargs["environment"]["SSH_USER_PASSWORD_HASH"] = unix_password_hash
            else:
                # No derived credential yet (pre-upgrade account that has not
                # logged in since): fall back to a per-session password so SSH
                # still works instead of silently locking the user out.
                ssh_password = generate_ssh_password()
                kwargs["environment"]["SSH_USER_PASSWORD"] = ssh_password

    # ── Jupyter authentication ───────────────────────────────────────────
    # The value arriving here is an argon2 HASH (never plaintext).  Two modes:
    #   * derived from the account password  → set alongside the session token,
    #     password_required=False, so the proxy can wave an authenticated user
    #     straight through while the form still accepts their account password;
    #   * explicitly chosen by the user      → password_required=True, a real
    #     second factor that the proxy does not bypass.
    if jupyter_password:
        kwargs["environment"]["JUPYTER_PASSWORD"] = jupyter_password
        kwargs["environment"]["JUPYTER_PASSWORD_REQUIRED"] = (
            "1" if jupyter_password_required else "0"
        )
    # The backend is the source of truth for this file; the entrypoint only
    # writes it when it is missing, so a password changed mid-session is not
    # overwritten by the container's stale copy on a self-heal restart.
    write_jupyter_auth_config(username, jupyter_password, jupyter_password_required)

    # ── Resource limits via cgroups ──────────────────────────────────────
    if memory_limit_mb:
        mem_bytes = memory_limit_mb * 1024 * 1024
        kwargs["mem_limit"] = mem_bytes
        kwargs["memswap_limit"] = mem_bytes          # no swap to evade the cap
        kwargs["mem_reservation"] = mem_bytes // 2

    kwargs.update(_disk_io_limits_kwargs())

    # CPU cap, in cores.  cpu_limit_seconds is deliberately NOT consulted here:
    # it is a cumulative CPU-time budget (RLIMIT_CPU) that cgroups cannot
    # express, and the old `seconds / 3600` translation turned an admin's "4"
    # into 0.25 cores, a container throttled to a quarter of a core while the
    # UI claimed four.
    cpu_cores = float(cpu_cores or settings.DEFAULT_CPU_CORES or 0)
    if cpu_cores > 0:
        kwargs["nano_cpus"] = int(cpu_cores * 1e9)

    container, ssh_port = _run_with_fallbacks(client, kwargs, name, candidates)
    logger.info(
        "Started container %s (id=%s) image=%s gpus=%r mem=%s cpus=%s ssh=%s",
        name, container.short_id, chosen_image, device_ids, memory_limit_mb,
        cpu_cores, ssh_port,
    )
    return {
        "container_id": container.id,
        "name": name,
        "status": "running",
        "image": chosen_image,
        "ssh_port": ssh_port,
        "ssh_password": ssh_password if ssh_port else None,
    }


_IO_KEYS = (
    "blkio_weight", "device_read_bps", "device_write_bps",
    "device_read_iops", "device_write_iops",
)


def _is_port_conflict(message: str) -> bool:
    lowered = message.lower()
    return (
        "port is already allocated" in lowered
        or "address already in use" in lowered
        or "bind for" in lowered
    )


def _run_with_fallbacks(client, kwargs: Dict[str, Any], name: str, ssh_ports: List[int]):
    """Create the container, recovering from the survivable failures.

    * **Unsupported disk-I/O knobs**.  Storage backends differ in what they
      will take, and a knob the daemon refuses would otherwise cost the user
      their session.  Drop the rejected one and retry instead.
    * **SSH port already taken**: another process grabbed the port between
      our scan and the create call.  Walk to the next candidate.
    * **No init binary**: the daemon cannot inject one.  Start without it;
      a session that leaks zombies still beats no session at all.

    Returns ``(container, ssh_port_or_None)``.
    """
    import docker

    attempt = dict(kwargs)
    port_queue = list(ssh_ports)
    ssh_port: Optional[int] = None
    if port_queue:
        ssh_port = port_queue.pop(0)
        attempt["ports"] = {"22/tcp": ("0.0.0.0", ssh_port)}

    while True:
        try:
            return client.containers.run(**attempt), ssh_port
        except docker.errors.APIError as exc:
            message = str(exc)
            # A failed create can leave a container holding the name
            try:
                client.containers.get(name).remove(force=True)
            except Exception:  # noqa: BLE001 (already gone is fine)
                pass

            if _is_port_conflict(message) and port_queue:
                ssh_port = port_queue.pop(0)
                attempt["ports"] = {"22/tcp": ("0.0.0.0", ssh_port)}
                logger.warning("SSH port busy, retrying on %d", ssh_port)
                continue
            if _is_port_conflict(message):
                logger.warning("SSH port range exhausted, starting without SSH")
                attempt.pop("ports", None)
                ssh_port = None
                continue

            # A device node the daemon will not take costs the session
            # nothing: dropping the list only gives up the protection against
            # systemd revoking the GPU later, and the runtime hook still
            # injects the devices themselves.
            if attempt.get("devices") and (
                "device" in message.lower() and "nvidia" in message.lower()
            ):
                attempt.pop("devices")
                logger.warning(
                    "Docker daemon rejected the GPU device list, starting "
                    "without it; the container will lose its GPU if systemd "
                    "reapplies its device policy (%s)",
                    message.splitlines()[0][:160],
                )
                continue

            # An old or unusual daemon may have no init binary to inject.
            # Without one the zombies come back and the workspace eventually
            # stops accepting SSH, but refusing to start it at all is worse.
            if attempt.get("init") and "init" in message.lower():
                attempt.pop("init")
                logger.warning(
                    "Docker daemon has no init binary, starting without one; "
                    "SSH logins will leak zombies (%s)",
                    message.splitlines()[0][:160],
                )
                continue

            victim = next(
                (key for key in _IO_KEYS if key in attempt and (
                    key.replace("_", ".") in message
                    or "blkio" in message
                    or "io.weight" in message
                    or "io.max" in message
                    or "device_read" in message
                    or "device_write" in message
                )),
                None,
            )
            if victim is None:
                raise  # unrelated failure, surface it
            attempt.pop(victim)
            logger.warning(
                "Docker daemon rejected %s, retrying without it (%s)",
                victim, message.splitlines()[0][:160],
            )


def _remove_existing(client, name: str) -> None:
    try:
        old = client.containers.get(name)
        logger.info("Removing stale container %s", name)
        old.remove(force=True)
    except Exception:  # noqa: BLE001 (not found is fine)
        pass


def update_container_password(username: str, unix_password_hash: Optional[str]) -> bool:
    """Change the UNIX password inside a running container (no restart).

    Called when the account password changes so SSH does not keep accepting the
    old one, and so the user is never told to restart a session to fix login.
    """
    if not unix_password_hash:
        return False
    try:
        container = _docker_client().containers.get(container_name(username))
        if container.status != "running":
            return False
        safe = sanitize_username(username)
        # `chpasswd -e` consumes a pre-hashed value, so no plaintext crosses the
        # Docker API.  The digest is passed as a positional argument rather than
        # interpolated into the shell string, so nothing has to be escaped and
        # it cannot be re-read from the command line of a long-lived process.
        rc, out = container.exec_run(
            ["sh", "-c", 'printf "%s:%s\n" "$1" "$2" | chpasswd -e',
             "chpasswd-wrapper", safe, unix_password_hash],
            user="root",
        )
        if rc == 0:
            logger.info("Updated container password for %r", username)
            return True
        logger.warning(
            "chpasswd failed for %r: %s", username,
            (out or b"").decode(errors="replace")[:200],
        )
        return False
    except Exception as exc:  # noqa: BLE001 (no live container is fine)
        logger.debug("Could not update container password for %r: %s", username, exc)
        return False


def get_container_state(username: str) -> Optional[Dict[str, Any]]:
    """Return docker-reported state for the user's container (or None)."""
    try:
        client = _docker_client()
        container = client.containers.get(container_name(username))
        container.reload()
        state = container.attrs.get("State", {})
        return {
            "running": bool(state.get("Running")),
            "exit_code": state.get("ExitCode"),
            "oom_killed": bool(state.get("OOMKilled")),
            "status": container.status,
            "container_id": container.id,
            "started_at": state.get("StartedAt"),
            "image": (container.image.tags or [container.image.short_id])[0]
            if container.image else None,
        }
    except RuntimeError:
        raise
    except Exception:  # noqa: BLE001 (container not found, daemon hiccup)
        return None


def stop_user_container(username: str, timeout: int = 15) -> bool:
    """Stop and remove the user's container. True when it is gone."""
    try:
        client = _docker_client()
        name = container_name(username)
        try:
            container = client.containers.get(name)
            container.stop(timeout=timeout)
            container.remove()
        except Exception:  # noqa: BLE001 (already gone)
            pass
        logger.info("Stopped container %s", name)
        return True
    except RuntimeError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed stopping container for %r: %s", username, exc)
        return False


def restart_user_container(username: str) -> bool:
    """Restart an existing container (self-heal path)."""
    try:
        client = _docker_client()
        container = client.containers.get(container_name(username))
        container.restart(timeout=10)
        logger.info("Restarted container for %r", username)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Restart failed for %r: %s", username, exc)
        return False


def ensure_containers_healthy(sessions: List[Any]) -> List[Dict[str, Any]]:
    """Self-heal pass over session rows marked running.

    Returns the list of repair actions taken: ``[{"user": ..., "action": ...}]``.
    """
    from models import SessionStatus

    actions: List[Dict[str, Any]] = []
    for session in sessions:
        if session.status != SessionStatus.running or not getattr(session, "container_id", None):
            continue
        state = get_container_state(session.user.username)
        if state is None:
            actions.append({"user": session.user.username, "action": "missing"})
            continue
        if not state["running"]:
            if state.get("oom_killed"):
                actions.append({
                    "user": session.user.username,
                    "action": "oom_killed",
                    "exit_code": state.get("exit_code"),
                })
            ok = restart_user_container(session.user.username)
            actions.append({
                "user": session.user.username,
                "action": "restarted" if ok else "restart_failed",
            })
    return actions


# ---------------------------------------------------------------------------
# SSH authorized_keys, installed live, not only at session start
# ---------------------------------------------------------------------------

def authorized_keys_path(username: str) -> str:
    """Backend-visible path of the authorized_keys the platform manages.

    In the platform's own directory, which is mounted at /platform beside the
    workspace and listed in sshd's AuthorizedKeysFile ahead of
    ~/.ssh/authorized_keys.  The user's own file is left alone: when a
    workspace is somebody's real home, truncating it would take away their
    access to this machine.
    """
    return os.path.join(
        settings.JUPYTER_DATA_DIR, username, ".platform", "authorized_keys"
    )


# Marker recording that the user's data directory has been re-owned to the
# container UID.  The recursive pass is done once, not on every session start.
_OWNERSHIP_MARKER = ".platform-ownership-v1"


def ensure_workspace_ownership(username: str, workspace=None) -> None:
    """Make the user's data directory writable by their container account.

    ``jupyter_data/<user>`` is bind-mounted at ``/workspace`` and is what
    JupyterLab serves.  The backend creates it as root, and on some hosts it
    ends up owned by whichever account ran the deploy, either way NOT the
    container's UID, so mode 0755 left the user able to read their own files
    but not create a notebook ("Permission denied" on every save).

    The top-level chown runs every start (cheap and always correct); the
    recursive repair runs once, guarded by a marker, because walking a
    directory holding datasets is not something to do on every launch.
    """
    if workspace is not None and workspace.mapped:
        # Somebody else's directory: the container account is created with the
        # uid that already owns it, and nothing is rewritten.  Chowning a
        # person's own home to the platform's uid would take their files away
        # from them on the host.
        return

    root = os.path.join(settings.JUPYTER_DATA_DIR, username)
    marker = os.path.join(root, _OWNERSHIP_MARKER)
    try:
        os.makedirs(root, exist_ok=True)
        os.chown(root, CONTAINER_UID, CONTAINER_UID)
    except OSError as exc:
        logger.warning("Could not chown %s: %s", root, exc)
        return

    if os.path.exists(marker):
        return

    try:
        # `chown -R` in C is far faster than walking the tree in Python, and a
        # first-time repair may cover a lot of files.
        result = subprocess.run(
            ["chown", "-R", f"{CONTAINER_UID}:{CONTAINER_UID}", root],
            capture_output=True, text=True, timeout=600,
        )
        if result.returncode == 0:
            with open(marker, "w", encoding="utf-8") as fh:
                fh.write("ownership normalised to uid %d\n" % CONTAINER_UID)
            os.chown(marker, CONTAINER_UID, CONTAINER_UID)
            logger.info("Normalised ownership of %s to uid %d", root, CONTAINER_UID)
        else:
            logger.warning("chown -R %s failed: %s", root, result.stderr.strip()[:200])
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("Ownership repair for %s failed: %s", root, exc)


def migrate_split_home(username: str) -> None:
    """Fold ``<user>/.home/*`` up into ``<user>/`` (one-time, idempotent).

    Earlier versions bind-mounted ``<user>/.home`` separately at
    ``/home/<user>``, which meant SSH and JupyterLab showed different trees:
    a directory created over SSH landed inside a hidden folder the notebook
    file browser does not display.  Now there is one directory, so anything a
    user already had, ``pip install --user`` packages in ``.local``, their
    ``.ssh``, ``.jupyter`` and shell dotfiles, has to move up one level.

    Entries are moved individually with rename (same filesystem, atomic); an
    entry whose destination already exists is left alone rather than
    overwriting newer data.  ``.home`` is removed only once it is empty.
    """
    root = os.path.join(settings.JUPYTER_DATA_DIR, username)
    legacy = os.path.join(root, ".home")
    if not os.path.isdir(legacy):
        return

    moved, skipped = 0, []
    try:
        for name in os.listdir(legacy):
            source = os.path.join(legacy, name)
            target = os.path.join(root, name)
            if os.path.exists(target):
                skipped.append(name)
                continue
            try:
                os.rename(source, target)
                moved += 1
            except OSError as exc:
                skipped.append(f"{name} ({exc})")
        try:
            os.rmdir(legacy)
        except OSError:
            pass  # still has entries we could not move, keep it for inspection
    except OSError as exc:
        logger.warning("Home migration for %r failed: %s", username, exc)
        return

    if moved or skipped:
        logger.info(
            "Merged split home for %r: %d entry(ies) moved into the workspace%s",
            username, moved,
            f", left in .home: {skipped}" if skipped else "",
        )


def write_platform_limits_file(username: str, disk_quota_mb: Optional[int],
                               owner: Optional[int] = None) -> bool:
    """Publish platform-level limits into the user's workspace.

    The cgroup already tells the container its RAM/CPU/PID/IO caps, but a disk
    quota the platform enforces in software has no kernel representation, so it
    has to be handed over explicitly.  A file rather than an environment
    variable, because env is fixed when the container is created and would go
    stale the moment an admin changed the quota.
    """
    from services import safepath

    from services import workspaces

    owner = workspaces.owner_uid(username) if owner is None else owner
    space = workspaces.for_username(username)
    body = ("# Written by the GPU Platform backend; do not edit.\n"
            f"PLATFORM_DISK_QUOTA_MB={int(disk_quota_mb or 0)}\n"
            f"PLATFORM_HOME={space.container_path}\n")
    try:
        safepath.write(
            os.path.join(settings.JUPYTER_DATA_DIR, username, ".platform"),
            "limits.env", body, mode=0o644, owner=owner,
        )
        return True
    except (OSError, safepath.UnsafePath) as exc:
        logger.warning("Could not write platform limits for %r: %s", username, exc)
        return False


def write_budget_file(username: str, state: Dict[str, Any],
                      owner: Optional[int] = None) -> bool:
    """Publish the user's GPU / CPU hour standing into their workspace.

    Separate from ``limits.env`` on purpose: that file holds ceilings, which
    change when an administrator changes them, while this one holds a count
    that moves every minute a session is open.  Keeping them apart means the
    fast-moving figures can be rewritten by the enforcement pass without any of
    the callers that only know about ceilings having to supply them.
    """
    from services import safepath, workspaces

    owner = workspaces.owner_uid(username) if owner is None else owner
    period = state.get("period") or {}
    gpu = state.get("gpu_hours") or {}
    cpu = state.get("cpu_hours") or {}

    def quoted(value) -> str:
        # The file is sourced by a shell, so every value is quoted and any
        # quote inside it dropped: "Mon 29 Sep at 00:00" is several words, and
        # unquoted the shell would try to run the second one as a command.
        return '"' + str(value if value is not None else "").replace('"', "") + '"'

    def hours(value) -> str:
        return f"{float(value or 0):g}"

    def ceiling(value) -> str:
        # Empty means no budget, which is not the same as a budget of zero.
        return "" if not value else f"{float(value):g}"

    body = (
        "# Written by the GPU Platform backend; do not edit.\n"
        f"PLATFORM_BUDGET_PERIOD={quoted(period.get('label'))}\n"
        f"PLATFORM_BUDGET_KIND={quoted(period.get('kind'))}\n"
        f"PLATFORM_BUDGET_RESETS={quoted(period.get('resets_text'))}\n"
        f"PLATFORM_GPU_HOURS_USED={quoted(hours(gpu.get('used')))}\n"
        f"PLATFORM_GPU_HOURS_QUOTA={quoted(ceiling(gpu.get('quota')))}\n"
        f"PLATFORM_CPU_HOURS_USED={quoted(hours(cpu.get('used')))}\n"
        f"PLATFORM_CPU_HOURS_QUOTA={quoted(ceiling(cpu.get('quota')))}\n"
    )
    try:
        safepath.write(
            os.path.join(settings.JUPYTER_DATA_DIR, username, ".platform"),
            "budget.env", body, mode=0o644, owner=owner,
        )
        return True
    except (OSError, safepath.UnsafePath) as exc:
        logger.debug("Could not write budget file for %r: %s", username, exc)
        return False


def write_job_token(username: str, token: str, owner: Optional[int] = None) -> bool:
    """Drop the job token where the CLI can read it.

    Into the platform's own directory, not the workspace: a workspace may be
    somebody's real home, and the platform has no business leaving files there.
    """
    from services import safepath, workspaces

    owner = workspaces.owner_uid(username) if owner is None else owner

    try:
        safepath.write(
            os.path.join(settings.JUPYTER_DATA_DIR, username, ".platform"),
            "job-token", token + "\n", mode=0o600, owner=owner,
        )
        return True
    except (OSError, safepath.UnsafePath) as exc:
        logger.warning("Could not write job token for %r: %s", username, exc)
        return False


def environment_is_current(username: str) -> Optional[bool]:
    """Is the user's workspace running the image the platform would build now?

    A stop/start recreates the container and therefore picks up whatever the
    image tag points at.  A *restart*, self-heal, or the daemon's restart
    policy, reuses the same container and keeps its original image, so a
    workspace can quietly run something months old.  Returning this lets the
    dashboard say so instead of leaving people to wonder.
    """
    try:
        client = _docker_client()
        container = client.containers.get(container_name(username))
        current = client.images.get(settings.JUPYTER_IMAGE)
        return container.image.id == current.id
    except Exception:  # noqa: BLE001 (no container, or image gone)
        return None


def job_container_name(job_id: int) -> str:
    return f"gpu-job-{job_id}"


def start_job_container(job, user) -> Dict[str, Any]:
    """Run a job's script in a container shaped like its owner's workspace.

    Same image, same workspace mount, same account, same caps.  The only
    differences are the GPUs (chosen by the scheduler, not the assignment) and
    that it runs one script instead of JupyterLab.  That is what makes "a job
    is limited exactly like the user who submitted it" true rather than
    aspirational.
    """
    from services import quota

    client = _docker_client()
    image = ensure_image(resolve_image(user.preferred_image))
    if image is None:
        raise RuntimeError(f"Jupyter image {settings.JUPYTER_IMAGE!r} is not available")

    name = job_container_name(job.id)
    _remove_existing(client, name)

    assignment = user.gpu_assignment
    memory_limit_mb = (assignment.memory_limit_mb if assignment else None) \
        or settings.DEFAULT_MEMORY_LIMIT_MB
    cpu_cores = float((assignment.cpu_cores if assignment else None)
                      or settings.DEFAULT_CPU_CORES or 0)
    max_processes = (assignment.max_processes if assignment else None) \
        or settings.CONTAINER_PIDS_LIMIT or None

    from services import workspaces

    space = workspaces.for_user(user)
    ensure_workspace_ownership(user.username, space)
    write_platform_limits_file(user.username, quota.disk_quota_mb(user), owner=space.uid)

    device_ids = [str(i) for i in (job.gpu_indices or "").split(",") if i.strip()]

    kwargs: Dict[str, Any] = {
        "image": image,
        "name": name,
        "hostname": "workspace",
        "labels": {
            LABEL_USER: user.username,
            LABEL_ROLE: "job",
            LABEL_JOB: str(job.id),
        },
        "environment": {
            "PIP_USER": "1",
            "SSH_ENABLED": "0",
            # The same home as the owner's workspace, so a script that worked
            # interactively works here too, same conda prefix, same ~/.local.
            "PLATFORM_HOME": space.container_path,
            "SSH_USER_NAME": user.username,
            "SSH_USER_UID": str(space.uid),
            "SSH_USER_GID": str(space.gid),
            # Switches the shared entrypoint from "serve JupyterLab" to "run
            # this script"; everything before that, account, home, env, is
            # identical, which is the point.
            "PLATFORM_JOB_ID": str(job.id),
            "PLATFORM_JOB_SCRIPT": job.script,
            "PLATFORM_JOB_WORKDIR": job.workdir or ".",
            "PLATFORM_JOB_OUTPUT": job.output_path or f"output.{job.id}.out",
        },
        "volumes": {
            space.host_path: {"bind": space.container_path, "mode": "rw"},
            f"{host_data_dir()}/{user.username}/.platform": {
                "bind": workspaces.PLATFORM_DIR, "mode": "rw",
            },
        },
        "network": settings.DOCKER_NETWORK,
        "detach": True,
        "auto_remove": False,
        # A job that dies stays dead: restarting it would silently re-run a
        # script that may already have had side effects.
        "restart_policy": {"Name": "no"},
        "security_opt": ["no-new-privileges:true"],
        # Same reasoning as the interactive workspace: no raw sockets.
        "cap_drop": ["NET_RAW"],
        # And the same reason for an init: a script that backgrounds work and
        # exits leaves orphans, and an orphan nobody reaps holds a pids.max
        # slot for the rest of the job.
        "init": True,
        "pids_limit": max_processes,
        "mem_limit": memory_limit_mb * 1024 * 1024,
        "memswap_limit": memory_limit_mb * 1024 * 1024,
        # A job runs the same workloads as the interactive workspace and needs
        # the same shared memory; the DataLoader does not care which it is in.
        "shm_size": shm_size_bytes(memory_limit_mb),
    }
    if cpu_cores > 0:
        kwargs["nano_cpus"] = int(cpu_cores * 1e9)
    kwargs.update(_disk_io_limits_kwargs())

    if device_ids:
        kwargs.update(_device_kwargs(device_ids))
        kwargs["environment"]["CUDA_VISIBLE_DEVICES"] = ",".join(
            str(i) for i in range(len(device_ids))
        )
        kwargs["environment"]["NVIDIA_DRIVER_CAPABILITIES"] = "compute,utility"
    else:
        kwargs["environment"]["NVIDIA_VISIBLE_DEVICES"] = "void"
        kwargs["environment"]["CUDA_VISIBLE_DEVICES"] = ""

    container, _ = _run_with_fallbacks(client, kwargs, name, [])
    logger.info(
        "Started job %d for %r on GPU(s) %s (mem=%sMB cpus=%s)",
        job.id, user.username, job.gpu_indices or "none", memory_limit_mb, cpu_cores,
    )
    return {"container_id": container.id, "name": name}


def job_container_state(job_id: int) -> Optional[Dict[str, Any]]:
    try:
        container = _docker_client().containers.get(job_container_name(job_id))
        container.reload()
        state = container.attrs.get("State", {})
        return {
            # Docker reports a paused container as Running, which is true of
            # the container and false of the work inside it.
            "running": bool(state.get("Running")),
            "paused": bool(state.get("Paused")),
            "exit_code": state.get("ExitCode"),
            "oom_killed": bool(state.get("OOMKilled")),
            "status": container.status,
        }
    except Exception:  # noqa: BLE001 (not found)
        return None


def stop_job_container(job_id: int, timeout: int = 10) -> bool:
    try:
        container = _docker_client().containers.get(job_container_name(job_id))
        # A frozen process cannot act on SIGTERM, so stopping a paused
        # container would wait out the whole timeout and then kill it.  Thaw it
        # first and it gets its chance to exit.
        if container.status == "paused":
            try:
                container.unpause()
            except Exception:  # noqa: BLE001 (raced with something else)
                pass
        container.stop(timeout=timeout)
        return True
    except Exception:  # noqa: BLE001 (already gone)
        return False


def pause_job_container(job_id: int) -> bool:
    """Freeze every process in the job's container (cgroup freezer).

    The job keeps its memory and its open files and resumes exactly where it
    stopped, which is why this is worth doing at all; it also keeps its RAM,
    which is why only CPU-only jobs are ever paused.
    """
    try:
        _docker_client().containers.get(job_container_name(job_id)).pause()
        return True
    except Exception as exc:  # noqa: BLE001 (gone, or already paused)
        logger.warning("Could not pause job %d: %s", job_id, exc)
        return False


def unpause_job_container(job_id: int) -> bool:
    try:
        _docker_client().containers.get(job_container_name(job_id)).unpause()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not resume job %d: %s", job_id, exc)
        return False


def remove_job_container(job_id: int) -> None:
    try:
        _docker_client().containers.get(job_container_name(job_id)).remove(force=True)
    except Exception:  # noqa: BLE001 (already gone)
        pass


def jupyter_auth_config_path(username: str) -> str:
    """Backend-visible path of the user's jupyter_server_config.json."""
    return os.path.join(
        settings.JUPYTER_DATA_DIR, username, ".platform",
        "jupyter_server_config.json",
    )


def write_jupyter_auth_config(
    username: str, hashed_password: Optional[str], password_required: bool = False,
    owner: Optional[int] = None,
) -> bool:
    """Write Jupyter's auth config into the user's home volume.

    The backend owns this file so a password change is not stranded: the
    container entrypoint bakes its copy at creation time, so without this the
    Jupyter login form kept accepting only the password the session started
    with.  Jupyter reads the file at startup, so the corrected value applies
    from the next session start (the dashboard link keeps working meanwhile,
    since the proxy authenticates with the session token, not the password).
    """
    from services import safepath, workspaces

    owner = workspaces.owner_uid(username) if owner is None else owner
    root = os.path.join(settings.JUPYTER_DATA_DIR, username, ".platform")
    try:
        if hashed_password:
            payload = {
                "IdentityProvider": {"hashed_password": hashed_password},
                "ServerApp": {"password_required": bool(password_required)},
            }
            if password_required:
                payload["ServerApp"]["token"] = ""
            safepath.write(root, "jupyter_server_config.json",
                           json.dumps(payload, indent=2), mode=0o600, owner=owner)
        else:
            safepath.remove(root, "jupyter_server_config.json")
        return True
    except (OSError, safepath.UnsafePath) as exc:
        logger.warning("Could not write Jupyter auth config for %r: %s", username, exc)
        return False


def install_authorized_keys(username: str, public_key: Optional[str],
                            owner: Optional[int] = None) -> bool:
    """Write (or remove) the user's authorized_keys so it applies immediately.

    sshd re-reads authorized_keys on every authentication attempt, so a key
    saved here works on the *current* session, no restart.  Previously the key
    was only passed to the container as an environment variable at creation
    time, which meant a key added after the session started silently did
    nothing and the user was stuck on password auth with no indication why.

    Ownership and modes matter: sshd's StrictModes rejects a key file that is
    group/world-writable or not owned by the authenticating user.
    """
    from services import safepath, workspaces

    owner = workspaces.owner_uid(username) if owner is None else owner
    root = os.path.join(settings.JUPYTER_DATA_DIR, username, ".platform")
    try:
        if public_key:
            safepath.write(root, "authorized_keys", public_key.strip() + "\n",
                           mode=0o600, owner=owner)
        else:
            safepath.remove(root, "authorized_keys")
        os.chmod(root, 0o755)
        os.chown(root, owner, owner)
        return True
    except (OSError, safepath.UnsafePath) as exc:
        # Not fatal: the key is in the database and the entrypoint installs it
        # at the next session start.
        logger.warning("Could not install authorized_keys for %r: %s", username, exc)
        return False


# ---------------------------------------------------------------------------
# Introspection used by the metrics collector
# ---------------------------------------------------------------------------

def effective_limits(username: str) -> Optional[Dict[str, Any]]:
    """What the Docker daemon actually applied to this user's container.

    Read back from the daemon rather than from platform settings, because a
    setting the daemon rejects is silently dropped so the session can still
    start.  This is what makes "is the limit real?" answerable without opening
    a shell.
    """
    try:
        container = _docker_client().containers.get(container_name(username))
        host = container.attrs.get("HostConfig", {})
        devices = host.get("DeviceRequests") or []
        gpu_ids: List[str] = []
        for request in devices:
            gpu_ids.extend(request.get("DeviceIDs") or [])
        read_bps = host.get("BlkioDeviceReadBps") or []
        write_bps = host.get("BlkioDeviceWriteBps") or []
        return {
            "memory_limit_mb": round(host["Memory"] / 1048576) if host.get("Memory") else None,
            "cpu_cores": round(host["NanoCpus"] / 1e9, 2) if host.get("NanoCpus") else None,
            "pids_limit": host.get("PidsLimit") or None,
            "disk_read_mbps": round(read_bps[0]["Rate"] / 1048576) if read_bps else None,
            "disk_write_mbps": round(write_bps[0]["Rate"] / 1048576) if write_bps else None,
            "gpus": gpu_ids,
        }
    except Exception:  # noqa: BLE001 (no container is not an error here)
        return None


def list_platform_containers(role: Optional[str] = "jupyter") -> List[Any]:
    """Running containers this platform owns.

    ``role=None`` includes batch jobs as well as interactive workspaces, which
    is what GPU attribution needs: a job holds a card just as firmly as a
    notebook does, and leaving it out made every GPU look unused.
    """
    try:
        client = _docker_client()
        label = f"{LABEL_ROLE}={role}" if role else LABEL_ROLE
        return client.containers.list(filters={"label": label})
    except Exception as exc:  # noqa: BLE001
        logger.debug("Could not list platform containers: %s", exc)
        return []


def remove_user_containers(username: str) -> int:
    """Stop and remove every container belonging to *username*.

    Includes ones that have already exited.  A job container is stopped when
    its job ends but never removed, because its exit status is read back from
    it, so an account that is going away would otherwise leave every job
    container it ever ran behind.  Returns how many were removed.
    """
    try:
        client = _docker_client()
        containers = client.containers.list(
            all=True, filters={"label": f"{LABEL_USER}={username}"}
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not list containers for %r: %s", username, exc)
        return 0

    removed = 0
    for container in containers:
        try:
            container.remove(force=True)
            removed += 1
        except Exception as exc:  # noqa: BLE001 (already gone, or racing)
            logger.debug("Could not remove %s: %s", getattr(container, "name", "?"), exc)
    if removed:
        logger.info("Removed %d container(s) for %r", removed, username)
    return removed


def container_gpu_memory_mb(container) -> Optional[int]:
    """GPU memory the processes inside *container* are holding.

    Asked of the container itself rather than of the host.  The backend runs in
    its own PID namespace, and nvidia-smi filters compute processes it cannot
    see there.  From inside the backend the list is simply empty, which made
    every attempt to attribute VRAM silently return nothing.  A container can
    always see its own processes.
    """
    try:
        code, out = container.exec_run(
            ["nvidia-smi", "--query-compute-apps=used_gpu_memory",
             "--format=csv,noheader,nounits"],
        )
    except Exception:  # noqa: BLE001 (container exited mid-scan)
        return None
    if code != 0:
        return None

    total = 0
    for line in (out or b"").decode(errors="replace").splitlines():
        line = line.strip()
        if line.isdigit():
            total += int(line)
    return total


def container_host_processes(container) -> Dict[int, str]:
    """``{host_pid: command line}`` for processes inside *container*.

    ``docker top`` reports PIDs as the host sees them, which is exactly what
    ``nvidia-smi --query-compute-apps`` returns, and that pairing is what lets the
    GPU monitor say *which user* owns a compute process.

    The command line comes from the same call on purpose: the backend runs in
    its own PID namespace, so ``/proc/<host pid>/cmdline`` does not exist for
    it and every container workload showed up as "unknown".
    """
    try:
        info = container.top(ps_args="-eo pid,args")
        rows = info.get("Processes") or []
        processes: Dict[int, str] = {}
        for row in rows:
            if not row:
                continue
            pid_cell = (row[0] or "").strip()
            if not pid_cell.isdigit():
                continue
            command = " ".join(c for c in row[1:] if c).strip()
            processes[int(pid_cell)] = command[:120] or "unknown"
        return processes
    except Exception:  # noqa: BLE001 (container exited mid-scan)
        return {}


def container_host_pids(container) -> List[int]:
    """Host-namespace PIDs running inside *container*."""
    return list(container_host_processes(container).keys())


# ---------------------------------------------------------------------------
# Startup monitoring (used by routers to give the UI truthful feedback)
# ---------------------------------------------------------------------------

_STARTUP_TIMEOUT = 60          # seconds to wait for the Jupyter HTTP endpoint
_STARTUP_POLL_INTERVAL = 1.0   # seconds between probes


def probe_jupyter_http(
    name: str, base_url: str = "/", token: Optional[str] = None
) -> bool:
    """True when the container answers on its Jupyter API (Docker DNS)."""
    try:
        import urllib.request
        from urllib.parse import quote

        base = base_url if base_url.startswith("/") else f"/{base_url}"
        if not base.endswith("/"):
            base += "/"
        url = f"http://{name}:{CONTAINER_PORT}{base}api/status"
        if token:
            url += f"?token={quote(token)}"
        with urllib.request.urlopen(url, timeout=2) as resp:  # nosec, internal DNS
            return resp.status == 200
    except Exception as exc:  # noqa: BLE001
        # A 403 means Jupyter is up but rejecting our probe (password mode), and
        # that still counts as "serving".
        code = getattr(exc, "code", None)
        return code in (401, 403)


def wait_until_running(
    name: str,
    timeout: int = _STARTUP_TIMEOUT,
    base_url: str = "/",
    token: Optional[str] = None,
) -> bool:
    """Block until the user container serves Jupyter (or the timeout expires)."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            container = _docker_client().containers.get(name)
            if container.status == "exited":
                return False
        except Exception:  # noqa: BLE001
            return False
        if probe_jupyter_http(name, base_url=base_url, token=token):
            return True
        time.sleep(_STARTUP_POLL_INTERVAL)
    return False


def get_container_logs(name: str, tail: int = 40) -> str:
    """Last ``tail`` lines of the container log ('' when unavailable)."""
    try:
        container = _docker_client().containers.get(name)
        return (container.logs(tail=tail, timestamps=False) or b"").decode(
            "utf-8", errors="replace"
        )
    except Exception:  # noqa: BLE001
        return ""
