"""GPU monitoring service.

Queries ``nvidia-smi`` via subprocess to gather per-GPU statistics and
per-process memory usage, and attributes every compute process to the platform
user whose container owns it.

Mock data is **opt-in** (``ALLOW_MOCK_GPU=true``): silently inventing GPUs on a
production host let an admin assign GPU 3 on a 2-GPU machine and made the
dashboard look healthy while nothing worked.
"""

import logging
import os
import random
import re
import signal
import subprocess
import time
from typing import Any, Dict, List, Optional

from config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

#: Why the last nvidia-smi call failed, for the caller that has to explain it.
_LAST_ERROR: Dict[str, Optional[str]] = {"message": None}


def _run(cmd: List[str], timeout: int = 10) -> Optional[str]:
    """Run *cmd* and return stdout, or None on any failure."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            _LAST_ERROR["message"] = detail[0][:200] if detail else "non-zero exit"
            logger.debug("nvidia-smi returned non-zero: %s", _LAST_ERROR["message"])
            return None
        _LAST_ERROR["message"] = None
        return result.stdout
    except FileNotFoundError:
        _LAST_ERROR["message"] = "nvidia-smi is not installed in this container"
        logger.debug("nvidia-smi not found")
        return None
    except subprocess.TimeoutExpired:
        _LAST_ERROR["message"] = f"nvidia-smi did not answer within {timeout}s"
        logger.warning("nvidia-smi timed out")
        return None
    except Exception as exc:  # noqa: BLE001
        _LAST_ERROR["message"] = str(exc)[:200]
        logger.warning("nvidia-smi error: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Telemetry state
#
# A failed nvidia-smi is not the same fact as "this machine has no GPUs", and
# the platform used to report them identically: one unreadable call and every
# dashboard said the cards were gone, which is exactly what it looks like when
# systemd revokes a running container's device access.  The last good reading
# is kept so the UI can show the cards and label them stale, and the scheduler
# is told separately never to place work against a figure it cannot refresh.
# ---------------------------------------------------------------------------

_TELEMETRY: Dict[str, Any] = {"at": 0.0, "gpus": [], "failing_since": None,
                              "error": None}
#: A reading older than this is no basis for deciding where a job fits.
_FRESH_FOR = 120


def _note_success(gpus: List[Dict[str, Any]]) -> None:
    if _TELEMETRY["failing_since"] is not None:
        logger.warning("nvidia-smi is answering again after %.0f s",
                       time.monotonic() - _TELEMETRY["failing_since"])
    _TELEMETRY.update({"at": time.monotonic(), "gpus": gpus,
                       "failing_since": None, "error": None})


def _note_failure() -> None:
    if _TELEMETRY["failing_since"] is None:
        _TELEMETRY["failing_since"] = time.monotonic()
        logger.error(
            "Cannot read the GPUs from this container: %s. The host may still "
            "be fine: this is what it looks like when systemd reapplies the "
            "container's device policy and revokes /dev/nvidia*. Recreating "
            "the backend container restores it (./deploy.sh).",
            _LAST_ERROR["message"] or "nvidia-smi gave no output",
        )
    _TELEMETRY["error"] = _LAST_ERROR["message"]


def telemetry() -> Dict[str, Any]:
    """Whether the GPU figures can be trusted, and since when they could not."""
    failing_since = _TELEMETRY["failing_since"]
    age = (time.monotonic() - _TELEMETRY["at"]) if _TELEMETRY["at"] else None
    return {
        "ok": failing_since is None,
        "fresh": failing_since is None and age is not None and age <= _FRESH_FOR,
        "age_seconds": int(age) if age is not None else None,
        "failing_for_seconds": (
            int(time.monotonic() - failing_since) if failing_since else None
        ),
        "error": _TELEMETRY["error"],
        "ever_seen": bool(_TELEMETRY["gpus"]),
    }


def nvidia_smi_available() -> bool:
    """True when this host can actually report GPU state."""
    return _run(["nvidia-smi", "--query-gpu=index", "--format=csv,noheader"]) is not None


_UUID_CACHE: Dict[str, Any] = {"at": 0.0, "by_uuid": {}, "by_index": {}}
_UUID_TTL = 300  # GPU topology does not change while the host is up


def _refresh_uuid_maps() -> None:
    out = _run(["nvidia-smi", "--query-gpu=index,uuid", "--format=csv,noheader"])
    by_uuid: Dict[str, int] = {}
    by_index: Dict[int, str] = {}
    if out:
        for line in out.strip().splitlines():
            parts = [p.strip() for p in line.split(",", 1)]
            if len(parts) == 2:
                try:
                    index = int(parts[0])
                except ValueError:
                    continue
                by_uuid[parts[1]] = index
                by_index[index] = parts[1]
    _UUID_CACHE.update({"at": time.monotonic(), "by_uuid": by_uuid, "by_index": by_index})


def _maps() -> Dict[str, Any]:
    if time.monotonic() - _UUID_CACHE["at"] > _UUID_TTL:
        _refresh_uuid_maps()
    return _UUID_CACHE


def uuid_by_index() -> Dict[int, str]:
    """``{0: 'GPU-abc…'}``, used to pin containers to a *stable* device id."""
    return dict(_maps()["by_index"])


def index_by_uuid() -> Dict[str, int]:
    return dict(_maps()["by_uuid"])


# ---------------------------------------------------------------------------
# Device nodes
#
# Which /dev/nvidiaN belongs to which GPU index.  The two are the same number
# on an ordinary host, but they are not the same thing: the index is
# nvidia-smi's enumeration order and the minor is the driver's, and a wrong
# guess here would put another card's device node into somebody's container.
# The driver publishes the real answer, so it is read rather than assumed.
# ---------------------------------------------------------------------------

_MINOR_CACHE: Dict[str, Any] = {"at": 0.0, "by_index": {}}


def _pci_to_minor() -> Dict[str, int]:
    """``{'0000:01:00.0': 0}`` straight from the driver's own view."""
    out: Dict[str, int] = {}
    root = "/proc/driver/nvidia/gpus"
    try:
        entries = os.listdir(root)
    except OSError:
        return out
    for entry in entries:
        try:
            with open(os.path.join(root, entry, "information")) as fh:
                text = fh.read()
        except OSError:
            continue
        match = re.search(r"Device Minor:\s*(\d+)", text)
        if match:
            out[entry.lower()] = int(match.group(1))
    return out


def device_minor_by_index() -> Dict[int, int]:
    """``{gpu index: device minor}``, for naming /dev/nvidiaN correctly.

    Falls back to "the minor is the index", which is true on every ordinary
    host, and says so in the log when it has to.
    """
    if time.monotonic() - _MINOR_CACHE["at"] < _UUID_TTL and _MINOR_CACHE["by_index"]:
        return dict(_MINOR_CACHE["by_index"])

    by_index: Dict[int, int] = {}
    minors = _pci_to_minor()
    listing = _run(["nvidia-smi", "--query-gpu=index,pci.bus_id", "--format=csv,noheader"])
    for line in (listing or "").strip().splitlines():
        parts = [p.strip() for p in line.split(",", 1)]
        if len(parts) != 2:
            continue
        try:
            index = int(parts[0])
        except ValueError:
            continue
        # nvidia-smi writes 00000000:01:00.0, the driver tree 0000:01:00.0.
        bus = parts[1].lower()
        if bus.count(":") == 2 and len(bus.split(":")[0]) > 4:
            bus = bus.split(":", 1)[1]
            bus = f"{'0' * 4}:{bus}"
        if bus in minors:
            by_index[index] = minors[bus]
        else:
            logger.debug("No driver entry for GPU %d at %s, assuming minor %d",
                         index, bus, index)
            by_index[index] = index

    if by_index:
        _MINOR_CACHE.update({"at": time.monotonic(), "by_index": by_index})
    return dict(by_index)


# Command lines are shown to admins in the GPU monitor, and a Jupyter process
# carries its session token right there in argv.  Redact anything that looks
# like a credential before it reaches the UI.
_SECRET_ARG_RE = re.compile(
    r"((?:--)?[\w.]*(?:token|password|secret|key)[\w.]*[=\s])(\S+)",
    re.IGNORECASE,
)


def redact_command(command: str) -> str:
    """Mask credential-looking arguments in a process command line."""
    return _SECRET_ARG_RE.sub(lambda m: m.group(1) + "***", command or "")


def _get_process_name(pid: int, known: Optional[str] = None) -> str:
    """Command line of *pid*.

    Prefers the value harvested from the owning container (``docker top``):
    the backend lives in its own PID namespace, so procfs has no entry for a
    host PID and the /proc path only works in process-backend deployments.
    """
    if known:
        return redact_command(known)
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            raw = fh.read().replace(b"\x00", b" ").decode(errors="replace").strip()
        return redact_command(raw[:200]) if raw else "unknown"
    except OSError:
        return "unknown"


# ---------------------------------------------------------------------------
# GPU process → platform user attribution
# ---------------------------------------------------------------------------

_OWNER_CACHE: Dict[str, Any] = {"at": 0.0, "map": {}}
_OWNER_TTL = 10  # seconds, `docker top` on every container is not free


def platform_gpu_usage() -> Dict[int, Dict[str, int]]:
    """``{gpu index: {username: MB}}`` measured from inside each container.

    nvidia-smi run here lists nothing: this process is in its own PID
    namespace and the driver hides compute processes it cannot see, so asking
    the host-wide view for "which user" always came back empty.  Each
    container can see its own processes, and the platform already knows which
    physical GPU each one was pinned to.
    """
    usage: Dict[int, Dict[str, int]] = {}
    try:
        from services import container_manager

        # role=None: a batch job holds a card exactly as a workspace does.
        for container in container_manager.list_platform_containers(role=None):
            username = (container.labels or {}).get(container_manager.LABEL_USER)
            if not username:
                continue
            megabytes = container_manager.container_gpu_memory_mb(container)
            if not megabytes:
                continue
            # The container sees its GPUs renumbered from 0; the device request
            # records which physical cards those are.
            requests = (container.attrs.get("HostConfig") or {}).get("DeviceRequests") or []
            uuids = [u for r in requests for u in (r.get("DeviceIDs") or [])]
            by_uuid = index_by_uuid()
            indices = [by_uuid[u] for u in uuids if u in by_uuid]
            if not indices:
                continue
            share = megabytes // len(indices)
            for index in indices:
                usage.setdefault(index, {})[username] = (
                    usage.setdefault(index, {}).get(username, 0) + share
                )
    except Exception as exc:  # noqa: BLE001 (docker unavailable)
        logger.debug("Per-container GPU usage unavailable: %s", exc)
    return usage


def pid_owner_map() -> Dict[int, Dict[str, str]]:
    """``{host_pid: {"user": ..., "cmd": ...}}`` for every user container.

    ``nvidia-smi`` reports host-namespace PIDs while user workloads live in
    their own PID namespace, so a raw ``/proc`` lookup used to yield
    ``unknown`` for everything.  ``docker top`` bridges the two namespaces.
    """
    if time.monotonic() - _OWNER_CACHE["at"] < _OWNER_TTL:
        return _OWNER_CACHE["map"]

    owners: Dict[int, Dict[str, str]] = {}
    try:
        from services import container_manager

        # role=None: a batch job's processes hold a card exactly as a
        # workspace's do.  Filtering to "jupyter" left every job container
        # unattributed, and its work was then reported as somebody else's.
        for container in container_manager.list_platform_containers(role=None):
            username = (container.labels or {}).get(container_manager.LABEL_USER)
            if not username:
                continue
            for pid, command in container_manager.container_host_processes(container).items():
                owners[pid] = {"user": username, "cmd": command}
    except Exception as exc:  # noqa: BLE001 (docker unavailable (process mode))
        logger.debug("PID owner map unavailable: %s", exc)

    _OWNER_CACHE.update({"at": time.monotonic(), "map": owners})
    return owners


_UID_MAP_CACHE: Dict[str, Any] = {"at": 0.0, "accounts": {"by_uid": {}, "names": set()}}
_UID_MAP_TTL = 60


def platform_usernames() -> set:
    """Every account this platform knows about."""
    return set(_platform_accounts()["names"])


def _os_username(uid: int) -> Optional[str]:
    try:
        import pwd

        return pwd.getpwuid(uid).pw_name
    except (KeyError, ValueError):
        return None


def platform_uid_map() -> Dict[int, str]:
    """``{host uid: platform username}``, for users mapped to a real home.

    Work started over SSH rather than through a workspace runs as an ordinary
    host account, so the only honest way to say whose it is is the uid an
    administrator already tied to that account by mapping their home directory.

    Users without a mapping are deliberately absent: their workspace uid is the
    container's own, which belongs to nobody on the host, and claiming host
    processes with it would put somebody else's work under their name.
    """
    return _platform_accounts()["by_uid"]


def _platform_accounts() -> Dict[str, Any]:
    """``{"by_uid": {uid: username}, "names": {username}}``, cached."""
    if time.monotonic() - _UID_MAP_CACHE["at"] < _UID_MAP_TTL:
        return _UID_MAP_CACHE["accounts"]

    accounts: Dict[str, Any] = {"by_uid": {}, "names": set()}
    try:
        import models
        from database import SessionLocal
        from services import workspaces

        db = SessionLocal()
        try:
            for user in db.query(models.User).filter(models.User.deleted_at.is_(None)).all():
                accounts["names"].add(user.username)
                space = workspaces.for_user(user)
                if space.mapped:
                    accounts["by_uid"][space.uid] = user.username
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001 (never fail a status read on the DB)
        logger.debug("Could not read the platform accounts: %s", exc)

    _UID_MAP_CACHE.update({"at": time.monotonic(), "accounts": accounts})
    return accounts


def process_uid(pid: int) -> Optional[int]:
    """Real uid of *pid*, when this process can see the host's procfs."""
    try:
        with open(f"/proc/{pid}/status") as fh:
            for line in fh:
                if line.startswith("Uid:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _process_owner(pid: int, owners: Dict[int, Dict[str, str]]) -> Optional[str]:
    """Owner of *pid*, or None when this work is not the platform's.

    Three ways, most trustworthy first:

    * the platform container it runs in, a workspace or a batch job;
    * the uid an administrator tied to an account by mapping its home;
    * the host account it runs as, when that account is also a platform
      account.  Someone who works over SSH instead of through a workspace is
      still one of this platform's users, and their run showed up as a
      stranger's purely because nobody had mapped their home.

    That last rule takes a host account and a platform account with the same
    name to be the same person, which is the assumption this platform already
    makes everywhere else it hands a user their own files.
    """
    if pid in owners:
        return owners[pid]["user"]

    uid = process_uid(pid)
    if uid is None:
        return None

    mapped = platform_uid_map().get(uid)
    if mapped:
        return mapped

    name = _os_username(uid)
    if not name:
        return None
    known = platform_usernames()
    if name in known:
        return name
    # Process backend: Jupyter runs as gpu_<username> on this host.
    if name.startswith("gpu_") and name[4:] in known:
        return name[4:]
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_gpu_status(detailed: bool = False) -> List[Dict[str, Any]]:
    """Return a list of GPU status dicts, one per physical GPU.

    Each entry carries the usual utilisation figures plus ``processes``, where
    every process is annotated with the platform ``user`` that owns it (or
    ``None`` for workloads outside the platform).

    A process line says who is holding the card and how much, which is what a
    user needs to make sense of a card they share.  The *command line* is a
    different matter, it names someone else's files and what they are working
    on, so it is only included when *detailed* is asked for, and only an
    administrator's endpoints ask.  The default is the safe one on purpose: a
    new caller that forgets leaks nothing.
    """
    gpu_out = _run([
        "nvidia-smi",
        "--query-gpu=index,uuid,name,memory.total,memory.used,memory.free,"
        "utilization.gpu,utilization.memory,temperature.gpu,power.draw",
        "--format=csv,noheader,nounits",
    ])
    if not gpu_out:
        # The cards did not go anywhere; the reading did.  Hand back the last
        # one that worked, labelled, so the dashboard shows the machine as it
        # was rather than an empty rack.  Callers that must not act on a stale
        # figure ask telemetry() first.
        if _TELEMETRY["gpus"]:
            _note_failure()
            return [dict(gpu, stale=True) for gpu in _TELEMETRY["gpus"]]
        # Simulated hardware is a deliberate mode, not a broken host: it is
        # the reading this deployment is meant to have, so the queue and the
        # dashboard carry on as normal.
        mock = _mock_gpu_status()
        if mock:
            return mock
        _note_failure()
        return []

    gpus: List[Dict[str, Any]] = []
    for line in gpu_out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 9:
            continue
        try:
            gpus.append({
                "index":              int(parts[0]),
                "uuid":               parts[1],
                "name":               parts[2],
                "total_memory_mb":    int(parts[3]),
                "used_memory_mb":     int(parts[4]),
                "free_memory_mb":     int(parts[5]),
                "gpu_utilization":    int(parts[6]),
                "memory_utilization": int(parts[7]),
                "temperature":        int(parts[8]),
                "power_watts":        int(float(parts[9])) if len(parts) > 9 and parts[9].replace(".", "").isdigit() else None,
                "processes":          [],
                "users":              [],
                "mock":               False,
            })
        except (ValueError, IndexError) as exc:
            logger.warning("Could not parse GPU line %r: %s", line, exc)

    if not gpus:
        return _mock_gpu_status()

    position_by_index = {g["index"]: pos for pos, g in enumerate(gpus)}
    uuid_map = index_by_uuid()
    owners = pid_owner_map()

    # Attribution measured inside the containers, because the host-wide process
    # list is invisible from this PID namespace.
    for index, per_user in platform_gpu_usage().items():
        pos = position_by_index.get(index, -1)
        if pos < 0:
            continue
        gpus[pos]["platform_usage_mb"] = per_user
        for username in per_user:
            if username not in gpus[pos]["users"]:
                gpus[pos]["users"].append(username)

    proc_out = _run([
        "nvidia-smi",
        "--query-compute-apps=gpu_uuid,pid,used_gpu_memory",
        "--format=csv,noheader,nounits",
    ])
    if proc_out:
        for line in proc_out.strip().splitlines():
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 3:
                continue
            gpu_uuid = parts[0]
            try:
                pid = int(parts[1])
                mem_used = int(parts[2]) if parts[2].isdigit() else 0
            except ValueError:
                continue

            pos = position_by_index.get(uuid_map.get(gpu_uuid, -1), -1)
            if pos < 0:
                continue
            owner = _process_owner(pid, owners)
            entry: Dict[str, Any] = {
                "pid":            pid,
                "memory_used_mb": mem_used,
                "user":           owner,
            }
            if detailed:
                uid = process_uid(pid)
                entry["name"] = _get_process_name(pid, (owners.get(pid) or {}).get("cmd"))
                entry["uid"] = uid
                # Everything on the card is shown; only what the platform can
                # answer for may be stopped from here.  Work belonging to
                # another stack on this machine is somebody else's to manage.
                entry["stoppable"] = bool(owner) and uid not in (None, 0)
            gpus[pos]["processes"].append(entry)
            if owner and owner not in gpus[pos]["users"]:
                gpus[pos]["users"].append(owner)

    _note_success(gpus)
    return [dict(gpu, stale=False) for gpu in gpus]


def get_gpu_status_for(gpu_indices, detailed: bool = False) -> List[Dict[str, Any]]:
    """GPU status filtered to a set/list of indices.

    Filtering by index is not on its own enough to keep users apart: two of
    them can be assigned the same card, and then this returns the other one's
    processes.  What each line may say is decided by *detailed*, not by which
    card it is on.
    """
    allowed = {int(i) for i in gpu_indices}
    return [g for g in get_gpu_status(detailed=detailed) if g["index"] in allowed]


def _compute_pids() -> Dict[int, int]:
    """``{pid: MB}`` for every process currently holding GPU memory."""
    out = _run([
        "nvidia-smi", "--query-compute-apps=pid,used_gpu_memory",
        "--format=csv,noheader,nounits",
    ])
    live: Dict[int, int] = {}
    for line in (out or "").strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and parts[0].isdigit():
            live[int(parts[0])] = int(parts[1]) if parts[1].isdigit() else 0
    return live


def _container_main_pids() -> Dict[int, str]:
    """``{main pid: container name}`` for the platform's own containers."""
    mains: Dict[int, str] = {}
    try:
        from services import container_manager

        for container in container_manager.list_platform_containers(role=None):
            pid = ((container.attrs.get("State") or {}).get("Pid")) or 0
            if pid:
                mains[int(pid)] = container.name
    except Exception as exc:  # noqa: BLE001 (docker unavailable)
        logger.debug("Could not list container main pids: %s", exc)
    return mains


def stop_compute_process(pid: int, force: bool = False) -> Dict[str, Any]:
    """Signal a GPU compute process on this host.

    Everything on a card is shown to an administrator, but only work the
    platform can answer for may be stopped from here: a process it cannot
    attribute to one of its users belongs to another stack on this machine,
    and stopping it from this screen would be acting well outside what the
    platform was given.

    The live process list is read again rather than trusted from the caller.
    The screen it came from is seconds old, PIDs are reused, and the one thing
    worse than refusing to stop a job is stopping a different one.
    """
    from fastapi import HTTPException, status

    live = _compute_pids()
    if pid not in live:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Process {pid} is no longer using a GPU; it may have just finished.",
        )

    main_pids = _container_main_pids()
    if pid in main_pids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Process {pid} is what runs {main_pids[pid]} itself. Stop the "
                "workspace or cancel the job instead, so the platform can record "
                "the session ending."
            ),
        )

    owner = _process_owner(pid, pid_owner_map())
    uid = process_uid(pid)
    if not owner or uid in (None, 0):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Process {pid} does not belong to a platform user, so this "
                "platform will not stop it. It is another workload on this "
                "machine and whoever runs it manages it."
            ),
        )

    signal_sent = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.kill(pid, signal_sent)
    except ProcessLookupError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Process {pid} exited before it could be stopped.",
        )
    except PermissionError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                f"Not allowed to signal process {pid}. The backend needs the "
                "host PID namespace to reach it (docker-compose.gpu.yml)."
            ),
        )

    logger.info("Admin stopped GPU process %d (%s) owned by %r",
                pid, signal_sent.name, owner)
    return {
        "pid": pid,
        "user": owner,
        "signal": signal_sent.name,
        "memory_freed_mb": live[pid],
        "command": _get_process_name(pid, (pid_owner_map().get(pid) or {}).get("cmd")),
    }


# ---------------------------------------------------------------------------
# Mock data (development / CI without a GPU), opt-in only
# ---------------------------------------------------------------------------

def _mock_gpu_status() -> List[Dict[str, Any]]:
    """Two simulated GPUs, but only when explicitly allowed.

    On a real deployment an empty list is the honest answer: the UI then shows
    "no GPUs detected" instead of phantom hardware, and admins cannot assign
    GPUs that do not exist.
    """
    if not settings.ALLOW_MOCK_GPU:
        return []

    def _mock(index: int, base_used: int) -> Dict[str, Any]:
        total = 24576
        used = max(0, min(total, base_used + random.randint(-512, 512)))
        return {
            "index":              index,
            "uuid":               f"GPU-MOCK-{index}",
            "name":               f"NVIDIA GeForce RTX 3090 [mock #{index}]",
            "total_memory_mb":    total,
            "used_memory_mb":     used,
            "free_memory_mb":     total - used,
            "gpu_utilization":    random.randint(0, 80),
            "memory_utilization": round(used / total * 100),
            "temperature":        random.randint(38, 78),
            "power_watts":        random.randint(40, 320),
            "processes":          (
                [{"pid": 10000 + index, "name": "python training.py",
                  "memory_used_mb": used, "user": "mock-user"}]
                if used > 0 else []
            ),
            "users":              ["mock-user"] if used > 0 else [],
            "mock":               True,
        }

    return [_mock(0, 3200), _mock(1, 0)]
