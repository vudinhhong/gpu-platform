"""One nvidia-smi for the whole host, attributed back to containers.

Asking each container for its own VRAM costs a ``docker exec`` per job, and
the exec dominates: on the reference host it is 80-110 ms against 30-40 ms for
a single host-wide query that covers every card and every process at once.
The old way therefore grew with the number of jobs, which is what kept the
overrun check on a ten-second cycle -- and ten seconds is a long time for a
``cudaMalloc``.  A job could be several gigabytes past its allowance before
anything looked, and the neighbour the scheduler had fitted beside it on the
strength of its stated figure was the one that died of it.

This asks once and maps the answer back:

    nvidia-smi --query-compute-apps=pid,used_gpu_memory   ->  host PIDs
    /proc/<pid>/cgroup                                    ->  docker-<id>.scope

which needs the backend in the host PID namespace.  It already is, for the GPU
monitor, so nothing new is granted here.  Without it the driver reports no
process the backend cannot see, and this module says so rather than reporting
that every job holds nothing: `usable()` goes False and the caller falls back
to the per-container exec.
"""

import logging
import re
import subprocess
import threading
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)

#: 64 hex characters anywhere in a cgroup path is a container id, in both the
#: `/docker/<id>` and `/system.slice/docker-<id>.scope` spellings.
_CONTAINER_ID = re.compile(r"([0-9a-f]{64})")

_lock = threading.Lock()
_by_container: Dict[str, int] = {}
_taken_at: float = 0.0
_last_attempt: float = 0.0
_usable: bool = False
_last_error: Optional[str] = None


def _container_of(pid: str) -> Optional[str]:
    try:
        with open(f"/proc/{pid}/cgroup", "r") as fh:
            found = _CONTAINER_ID.search(fh.read())
    except OSError:
        # The process ended between the driver listing it and this read, or
        # the backend cannot see it.  Either way it cannot be attributed.
        return None
    return found.group(1) if found else None


def refresh(retry_after_failure: float = 0.0) -> Dict[str, int]:
    """Take one reading of every compute process on the host.

    Returns ``{container id: MB}``.  A container with no compute process is
    absent, which the reader must treat as zero and not as "cannot tell": a
    job that has released its memory is exactly the case the enforcement code
    has to see.

    *retry_after_failure* holds the next attempt back that many seconds once
    one has failed.  A caller on a one-second loop would otherwise ask a
    broken nvidia-smi once a second for as long as it stays broken, and a
    driver that is wedged rather than missing answers slowly, not quickly.
    """
    global _taken_at, _last_attempt, _usable, _last_error

    with _lock:
        if (not _usable and _last_attempt
                and (time.monotonic() - _last_attempt) < retry_after_failure):
            return {}
        _last_attempt = time.monotonic()

    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,used_gpu_memory",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        _mark_unusable("nvidia-smi is not installed in this container")
        return {}
    except Exception as exc:  # noqa: BLE001 (a loop must never die)
        _mark_unusable(str(exc))
        return {}

    if proc.returncode != 0:
        _mark_unusable((proc.stderr or "").strip() or "nvidia-smi returned non-zero")
        return {}

    totals: Dict[str, int] = {}
    listed = resolved = 0
    for line in proc.stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        listed += 1
        cid = _container_of(parts[0])
        if cid is None:
            continue
        resolved += 1
        totals[cid] = totals.get(cid, 0) + int(parts[1])

    # The driver named processes and not one of them could be traced to a
    # container: the backend is in its own PID namespace, or /proc is not
    # readable.  Reporting zero for every job would be worse than reporting
    # nothing, because zero is a number the enforcement code believes.
    if listed and not resolved:
        _mark_unusable("no compute process could be traced to a container; "
                       "is the backend in the host PID namespace?")
        return {}

    with _lock:
        _by_container.clear()
        _by_container.update(totals)
        _taken_at = time.monotonic()
        _usable = True
        _last_error = None
    return totals


def _mark_unusable(message: str) -> None:
    global _usable, _last_error
    with _lock:
        _usable = False
        _last_error = message
    logger.debug("GPU scan unusable: %s", message)


def usable(max_age_seconds: float = 30.0) -> bool:
    """Whether the last reading succeeded and is recent enough to act on."""
    with _lock:
        return _usable and (time.monotonic() - _taken_at) <= max_age_seconds


def container_mb(container_id: str, max_age_seconds: float = 30.0) -> Optional[int]:
    """VRAM held inside *container_id*, or None if the scan cannot say.

    Zero and None are different answers and the caller needs both: zero is a
    job holding nothing, None is a reading that did not happen.
    """
    if not container_id:
        return None
    with _lock:
        if not _usable or (time.monotonic() - _taken_at) > max_age_seconds:
            return None
        return _by_container.get(container_id[:64], 0)


def status() -> Dict[str, object]:
    """For the health endpoint and the logs: is this working, and how old."""
    with _lock:
        return {
            "usable": _usable,
            "age_seconds": (round(time.monotonic() - _taken_at, 2)
                            if _taken_at else None),
            "containers": len(_by_container),
            "error": _last_error,
        }
