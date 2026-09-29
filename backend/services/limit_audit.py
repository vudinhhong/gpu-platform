"""What the kernel is actually holding, against what Docker was told to apply.

The platform already reads a container's limits back from the daemon instead of
trusting the values it sent.  That catches a daemon which quietly drops a
setting, but not a daemon which accepts one and does not end up applying it:
`docker inspect` reports what Docker *recorded*, and the cgroup is a separate
thing that can be reset underneath it.  One workspace on the reference host ran
for nine days with `MemorySwap` equal to `Memory` in its HostConfig -- swap
disabled, as far as Docker was concerned -- and `memory.swap.max` at `max` in
the cgroup, which is swap very much enabled.  Nothing noticed, because nothing
was looking at the third column.

This is that third column.  The backend cannot read another container's cgroup:
it has a cgroup namespace of its own and sees only itself, so each container is
asked about its own limits instead, which it can always read at the root of its
`/sys/fs/cgroup`.

Divergence is reported, not repaired.  Re-applying a cgroup value under a
running container would leave Docker's record and the kernel disagreeing in the
other direction, and the honest fix is the one an administrator can already
do -- stop the workspace and start it again, which removes the container and
builds a new one.
"""

import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: How long a reading stands before the container is asked again.  Divergence
#: appears when a container starts, not while it runs, so this is slow on
#: purpose: the check costs an exec per container and nothing here is urgent.
TTL_SECONDS = 300.0

_lock = threading.Lock()
_cache: Dict[str, Dict[str, Any]] = {}

#: Read in one exec rather than one each.  A missing file prints its own error
#: to stderr and leaves the marker with nothing after it, which parses as absent.
_READ = (
    "for f in memory.max memory.swap.max cpu.max pids.max io.max; do "
    "  echo \"@@$f\"; cat /sys/fs/cgroup/$f 2>/dev/null; "
    "done; "
    "echo '@@gpu.nodes'; ls /dev/nvidia* 2>/dev/null; "
    # The exit code has to mean "the script ran", not "the last command found
    # something".  Without this, `ls` exiting 2 on a workspace with no GPU made
    # the whole reading look like a failure, and every GPU-less workspace was
    # skipped without a word -- the same confusion of absence with error that
    # this module exists to keep out of the findings.
    "exit 0"
)


def _read_cgroup(container) -> Optional[Dict[str, str]]:
    try:
        code, out = container.exec_run(["sh", "-c", _READ])
    except Exception:  # noqa: BLE001 (container exited mid-scan)
        return None
    if code != 0:
        return None

    values: Dict[str, str] = {}
    key = None
    for line in (out or b"").decode(errors="replace").splitlines():
        if line.startswith("@@"):
            key = line[2:].strip()
            values[key] = ""
        elif key:
            values[key] = (values[key] + " " + line.strip()).strip()
    return values


def _finding(setting: str, configured: str, kernel: str, detail: str) -> Dict[str, str]:
    return {"setting": setting, "configured": configured, "kernel": kernel,
            "detail": detail}


def _check(host: Dict[str, Any], cg: Dict[str, str]) -> List[Dict[str, str]]:
    """Compare one container's HostConfig against its own cgroup files."""
    out: List[Dict[str, str]] = []

    def num(raw: str) -> Optional[int]:
        raw = (raw or "").strip()
        if raw == "max":
            return None          # no limit, which is a value and not an absence
        return int(raw) if raw.isdigit() else None

    # ── memory ────────────────────────────────────────────────────────────
    want = host.get("Memory") or 0
    got = num(cg.get("memory.max", ""))
    if want and got != want:
        out.append(_finding(
            "memory", f"{want} B",
            "unlimited" if got is None else f"{got} B",
            "the container may use more memory than it was given"))

    # ── swap ──────────────────────────────────────────────────────────────
    # MemorySwap == Memory is Docker's way of saying "no swap on top", which
    # should land as memory.swap.max 0.  Anything else and the memory cap is
    # soft: the container spills to disk instead of being held to its figure,
    # and swap I/O is not charged to the container's io.max the way file I/O is,
    # so it lands on everybody.
    mem, swap = host.get("Memory") or 0, host.get("MemorySwap") or 0
    if mem and swap == mem:
        got = num(cg.get("memory.swap.max", ""))
        if got != 0:
            out.append(_finding(
                "swap", "disabled",
                "unlimited" if got is None else f"{got} B",
                "the memory cap is soft: this workspace can spill into host swap"))

    # ── cpu ───────────────────────────────────────────────────────────────
    nano = host.get("NanoCpus") or 0
    if nano:
        parts = (cg.get("cpu.max", "") or "").split()
        if len(parts) == 2 and parts[0] != "max" and parts[1].isdigit():
            cores = int(parts[0]) / int(parts[1])
            if abs(cores - nano / 1e9) > 0.01:
                out.append(_finding(
                    "cpu", f"{nano / 1e9:g} cores", f"{cores:g} cores",
                    "the container may use more cores than it was given"))
        else:
            out.append(_finding(
                "cpu", f"{nano / 1e9:g} cores", cg.get("cpu.max", "") or "unset",
                "no CPU ceiling is being applied"))

    # ── processes ─────────────────────────────────────────────────────────
    want = host.get("PidsLimit") or 0
    if want:
        got = num(cg.get("pids.max", ""))
        if got != want:
            out.append(_finding(
                "processes", str(want),
                "unlimited" if got is None else str(got),
                "the fork-bomb ceiling is not in place"))

    # ── disk throughput ───────────────────────────────────────────────────
    io = cg.get("io.max", "") or ""
    for key, field, label in (("BlkioDeviceReadBps", "rbps", "disk read"),
                              ("BlkioDeviceWriteBps", "wbps", "disk write")):
        for entry in (host.get(key) or []):
            rate = entry.get("Rate") or 0
            if rate and f"{field}={rate}" not in io:
                out.append(_finding(
                    label, f"{rate} B/s", io or "unset",
                    "the throughput cap is not in place"))

    # ── GPU device nodes ──────────────────────────────────────────────────
    # The NVIDIA runtime hook adds these behind the daemon's back, so systemd
    # never learns about them and the next time it reapplies the scope's device
    # policy they are revoked from a container that is still running.
    wanted_gpu = any(r.get("DeviceIDs") for r in (host.get("DeviceRequests") or []))
    if wanted_gpu and not (cg.get("gpu.nodes") or "").strip():
        out.append(_finding(
            "GPU devices", "granted by device request", "no /dev/nvidia* present",
            "the GPU has been revoked from this container; CUDA calls will fail"))

    return out


def audit(container, ttl: float = TTL_SECONDS) -> List[Dict[str, str]]:
    """Divergences for one container, cached.  Empty list means it agrees."""
    cid = getattr(container, "id", "") or ""
    now = time.monotonic()
    with _lock:
        hit = _cache.get(cid)
        if hit and (now - hit["at"]) < ttl:
            return hit["findings"]

    cg = _read_cgroup(container)
    if cg is None:
        findings: List[Dict[str, str]] = []      # cannot tell, so claim nothing
    else:
        try:
            findings = _check(container.attrs.get("HostConfig", {}) or {}, cg)
        except Exception as exc:  # noqa: BLE001 (never break the admin page)
            logger.debug("limit audit failed for %s: %s", cid[:12], exc)
            findings = []

    with _lock:
        _cache[cid] = {"at": now, "findings": findings}
    return findings


def audit_all() -> Dict[str, List[Dict[str, str]]]:
    """``{username: findings}`` for every workspace, skipping the ones that agree."""
    from services import container_manager

    out: Dict[str, List[Dict[str, str]]] = {}
    try:
        containers = container_manager.list_platform_containers()
    except Exception as exc:  # noqa: BLE001 (docker unavailable)
        logger.debug("limit audit: cannot list containers: %s", exc)
        return out

    for container in containers or []:
        username = (container.labels or {}).get(container_manager.LABEL_USER)
        if not username:
            continue
        findings = audit(container)
        if findings:
            out[username] = findings
    return out
