"""Resource telemetry: CPU, RAM, disk and per-user container usage.

The platform advertises itself as managing GPU **and** RAM/CPU, but nothing
ever measured the latter: the dashboard showed GPUs only, and an OOM kill was
invisible unless you read container logs.

Collection model
----------------
``docker stats`` needs ~1s per container and blocks, so it never runs inside a
request.  :func:`refresh` is called by a background task (see main.py) and
stores a snapshot; API handlers and the WebSocket feed read the cached value,
which is therefore always cheap and never blocks the event loop.
"""

import logging
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from config import settings

logger = logging.getLogger(__name__)

_snapshot: Dict[str, Any] = {"collected_at": None, "host": {}, "containers": []}
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Host-level
# ---------------------------------------------------------------------------

def _host_cpu_count() -> int:
    return os.cpu_count() or 1


_prev_cpu: Dict[str, float] = {}


def _host_cpu_percent() -> Optional[float]:
    """System-wide CPU usage from /proc/stat, sampled between refreshes."""
    try:
        with open("/proc/stat") as fh:
            fields = fh.readline().split()[1:]
        values = [float(v) for v in fields[:8]]
    except (OSError, ValueError, IndexError):
        return None

    total = sum(values)
    idle = values[3] + (values[4] if len(values) > 4 else 0.0)
    prev_total = _prev_cpu.get("total")
    prev_idle = _prev_cpu.get("idle")
    _prev_cpu["total"], _prev_cpu["idle"] = total, idle
    if prev_total is None or total <= prev_total:
        return None
    busy_delta = (total - prev_total) - (idle - prev_idle)
    return round(max(0.0, min(100.0, busy_delta / (total - prev_total) * 100)), 1)


def _host_memory() -> Dict[str, int]:
    info: Dict[str, int] = {}
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                value = rest.strip().split()[0]
                info[key] = int(value) // 1024  # kB → MiB
    except (OSError, ValueError, IndexError):
        return {}
    total = info.get("MemTotal", 0)
    available = info.get("MemAvailable", 0)
    return {
        "total_mb": total,
        "available_mb": available,
        "used_mb": max(0, total - available),
        "percent": round((total - available) / total * 100, 1) if total else 0.0,
    }


_GIB = 1024 ** 3


def _data_disk() -> Dict[str, Any]:
    """Free space on the volume holding the per-user data directory.

    Reported the way ``df`` reports it, deliberately:

    * **GiB, not decimal GB**, otherwise the dashboard and `df` disagree by 7%
      for no reason a reader can see.
    * **percent = used / (used + available)**, which is what `df` prints. A
      filesystem reserves blocks for root (5% by default on ext4, 46 GiB
      here), and counting those as free understated how full the disk was by
      more than five points, exactly when it mattered most.
    """
    try:
        usage = shutil.disk_usage(settings.JUPYTER_DATA_DIR)
    except OSError:
        return {}
    # shutil reports free = available-to-unprivileged, used = total - free_root
    denominator = usage.used + usage.free
    return {
        "total_gib": round(usage.total / _GIB, 1),
        "used_gib": round(usage.used / _GIB, 1),
        "free_gib": round(usage.free / _GIB, 1),
        "percent": round(usage.used / denominator * 100, 1) if denominator else 0.0,
        # Space a normal user can still write, which is what actually limits
        # them, the reserved blocks are not theirs to use.
        "available_gib": round(usage.free / _GIB, 1),
    }


def host_snapshot() -> Dict[str, Any]:
    return {
        "cpu_count": _host_cpu_count(),
        "cpu_percent": _host_cpu_percent(),
        "memory": _host_memory(),
        "data_disk": _data_disk(),
    }


# ---------------------------------------------------------------------------
# Per-container
# ---------------------------------------------------------------------------

def _cpu_percent(stats: Dict[str, Any]) -> Optional[float]:
    """Docker's own CPU-percent formula (cpu delta / system delta * cores)."""
    try:
        cpu = stats["cpu_stats"]
        pre = stats["precpu_stats"]
        cpu_delta = cpu["cpu_usage"]["total_usage"] - pre["cpu_usage"]["total_usage"]
        sys_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
        if cpu_delta <= 0 or sys_delta <= 0:
            return 0.0
        cores = cpu.get("online_cpus") or len(
            cpu["cpu_usage"].get("percpu_usage") or []
        ) or _host_cpu_count()
        return round(cpu_delta / sys_delta * cores * 100, 1)
    except (KeyError, TypeError, ZeroDivisionError):
        return None


def _memory(stats: Dict[str, Any]) -> Dict[str, Any]:
    try:
        mem = stats["memory_stats"]
        usage = mem.get("usage", 0)
        # Page cache is charged to the cgroup but is reclaimable, subtracting
        # it is what `docker stats` shows and what users recognise as "RAM used".
        cache = (mem.get("stats") or {}).get("inactive_file", 0)
        real = max(0, usage - cache)
        limit = mem.get("limit", 0)
        return {
            "used_mb": round(real / 1048576),
            "limit_mb": round(limit / 1048576) if limit else None,
            "percent": round(real / limit * 100, 1) if limit else None,
        }
    except (KeyError, TypeError, ZeroDivisionError):
        return {}


def _collect_one(container) -> Optional[Dict[str, Any]]:
    from services import container_manager

    username = (container.labels or {}).get(container_manager.LABEL_USER)
    if not username:
        return None
    try:
        stats = container.stats(stream=False)
    except Exception as exc:  # noqa: BLE001 (container died mid-scan)
        logger.debug("stats failed for %s: %s", container.name, exc)
        return None

    blkio = stats.get("blkio_stats", {}).get("io_service_bytes_recursive") or []
    read_bytes = sum(e.get("value", 0) for e in blkio if e.get("op", "").lower() == "read")
    write_bytes = sum(e.get("value", 0) for e in blkio if e.get("op", "").lower() == "write")

    return {
        "user": username,
        "container": container.name,
        "cpu_percent": _cpu_percent(stats),
        "memory": _memory(stats),
        "pids": (stats.get("pids_stats") or {}).get("current"),
        "disk_read_mb": round(read_bytes / 1048576, 1),
        "disk_write_mb": round(write_bytes / 1048576, 1),
    }


def refresh() -> Dict[str, Any]:
    """Collect a fresh snapshot (blocking, background task only)."""
    containers: List[Dict[str, Any]] = []
    try:
        from services import container_manager

        running = container_manager.list_platform_containers()
        if running:
            with ThreadPoolExecutor(max_workers=min(8, len(running))) as pool:
                containers = [c for c in pool.map(_collect_one, running) if c]
    except Exception as exc:  # noqa: BLE001 (docker unavailable)
        logger.debug("container metrics unavailable: %s", exc)

    snapshot = {
        "collected_at": time.time(),
        "host": host_snapshot(),
        "containers": sorted(containers, key=lambda c: c["user"]),
    }
    with _lock:
        _snapshot.clear()
        _snapshot.update(snapshot)
    return snapshot


def latest() -> Dict[str, Any]:
    """Most recent snapshot (never blocks; may be empty right after boot)."""
    with _lock:
        return dict(_snapshot)


def for_user(username: str) -> Optional[Dict[str, Any]]:
    for entry in latest().get("containers", []):
        if entry["user"] == username:
            return entry
    return None


# ---------------------------------------------------------------------------
# Disk usage per user (separate cadence, du is far more expensive)
# ---------------------------------------------------------------------------

_disk_cache: Dict[str, Any] = {"at": 0.0, "by_user": {}}


def set_user_disk_usage(username: str, megabytes: int) -> int:
    """Record a single measurement into the per-user view.

    A caller that re-measured one workspace publishes it here, so the next
    reader, the dashboard, the admin table, sees the same number instead of
    the one the last bulk scan left behind.
    """
    by_user = _disk_cache.get("by_user")
    if isinstance(by_user, dict):
        by_user[username] = megabytes
    return megabytes


def directory_size_mb(path: str, timeout: Optional[int] = None,
                      max_age: Optional[float] = None) -> int:
    """`du -sm` on one directory, cached like the per-user scan.

    Used for workspaces that live outside JUPYTER_DATA_DIR, which the bulk scan
    below never sees.

    *max_age* is how old a cached figure may be and still be returned.  The
    default TTL suits a scan nobody is waiting on; a caller about to refuse
    somebody over this number passes something short and pays for a fresh
    ``du`` instead of quoting a measurement the user has already invalidated.
    """
    cached = _disk_cache.setdefault("by_path", {})
    now = time.monotonic()
    ttl = settings.DISK_USAGE_TTL_SECONDS if max_age is None else max_age
    entry = cached.get(path)
    if entry and now - entry[0] < ttl:
        return entry[1]
    try:
        out = subprocess.run(
            ["du", "-sm", "--", path], capture_output=True, text=True,
            timeout=timeout or settings.DISK_USAGE_TIMEOUT_SECONDS,
        )
        value = int(out.stdout.split()[0]) if out.returncode == 0 and out.stdout.strip() else 0
    except (subprocess.SubprocessError, ValueError, OSError):
        value = 0
    cached[path] = (now, value)
    return value


def user_disk_usage(force: bool = False) -> Dict[str, int]:
    """``{username: megabytes}`` for every per-user data directory."""
    if not force and time.monotonic() - _disk_cache["at"] < settings.DISK_USAGE_TTL_SECONDS:
        return _disk_cache["by_user"]

    by_user: Dict[str, int] = {}
    root = settings.JUPYTER_DATA_DIR
    try:
        entries = [e for e in os.scandir(root) if e.is_dir() and e.name != "logs"]
    except OSError:
        entries = []

    for entry in entries:
        try:
            out = subprocess.run(
                ["du", "-sm", "--", entry.path],
                capture_output=True, text=True, timeout=settings.DISK_USAGE_TIMEOUT_SECONDS,
            )
            if out.returncode == 0 and out.stdout.strip():
                by_user[entry.name] = int(out.stdout.split()[0])
        except (subprocess.SubprocessError, ValueError, OSError):
            continue

    _disk_cache.update({"at": time.monotonic(), "by_user": by_user})
    return by_user
