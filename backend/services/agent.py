"""The node agent: what runs on a machine that has joined a master (ROLE=node).

It opens every connection itself (docs/multi-node.md).  First run: spend
JOIN_TOKEN at MASTER_URL and keep the credential it returns.  From then on two
threads:

* heartbeat, every NODE_HEARTBEAT_SECONDS: this node's GPUs, the VRAM each of
  its job containers holds (the same host-wide scan the master uses for its
  own GPUs), load and memory;
* commands: long-poll the master, run each command with services.node_ops,
  post the result.

Standard library HTTP only, so an agent needs nothing the backend image does
not already carry.
"""

import json
import logging
import os
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from config import settings

logger = logging.getLogger(__name__)


class Agent:
    def __init__(self, master_url: Optional[str] = None, credential_file: Optional[str] = None):
        self.master = (master_url or settings.MASTER_URL).rstrip("/")
        self.credential_file = credential_file or settings.NODE_CREDENTIAL_FILE
        self.name = settings.NODE_NAME or socket.gethostname()
        self.node_id: Optional[int] = None
        self.secret: Optional[str] = None
        self.proxy_key: Optional[str] = None
        self.stopping = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="node-op")
        # New on every start.  The master uses it to tell that commands it
        # handed to an earlier run of this agent will never be answered.
        self.boot = secrets.token_hex(8)

    # ── HTTP ────────────────────────────────────────────────────────────────
    def _http(self, method: str, path: str, body: Any = None, timeout: float = 30) -> Any:
        headers = {"Content-Type": "application/json"}
        if self.secret:
            headers["Authorization"] = f"Node {self.node_id}:{self.secret}"
        data = json.dumps(body, default=str).encode() if body is not None else None
        req = urllib.request.Request(self.master + path, data=data, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read()
        return json.loads(raw) if raw else None

    # ── joining ─────────────────────────────────────────────────────────────
    def _load(self) -> bool:
        try:
            with open(self.credential_file) as fh:
                c = json.load(fh)
            self.node_id, self.secret = int(c["node_id"]), c["secret"]
            self.proxy_key = c.get("proxy_key")
            return True
        except (OSError, ValueError, KeyError):
            return False

    def join(self, token: Optional[str] = None) -> None:
        token = token or settings.JOIN_TOKEN
        if not token:
            raise RuntimeError("no credential in %s and no JOIN_TOKEN to join with" % self.credential_file)
        res = self._http("POST", "/api/nodes/join", {
            "token": token, "name": self.name, "inventory": inventory(),
            "data_url": settings.NODE_DATA_URL or None})
        self.node_id, self.secret = int(res["node_id"]), res["secret"]
        self.proxy_key = res.get("proxy_key")
        os.makedirs(os.path.dirname(os.path.abspath(self.credential_file)), exist_ok=True)
        fd = os.open(self.credential_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump({"node_id": self.node_id, "secret": self.secret,
                       "proxy_key": self.proxy_key, "master": self.master,
                       "site": res.get("site")}, fh)
        logger.info("Joined %s as node %s (%r, site %r)", self.master, self.node_id,
                    self.name, res.get("site"))

    def ensure_joined(self) -> None:
        if not self._load():
            self.join()

    # ── loops ───────────────────────────────────────────────────────────────
    def heartbeat_once(self) -> Dict[str, Any]:
        return self._http("POST", "/api/nodes/heartbeat", telemetry(), timeout=15)

    def _heartbeat_loop(self) -> None:
        while not self.stopping.is_set():
            try:
                self.heartbeat_once()
            except Exception as exc:  # noqa: BLE001 (master away: keep trying)
                logger.warning("Heartbeat failed: %s", exc)
            self.stopping.wait(settings.NODE_HEARTBEAT_SECONDS)

    def _run_command(self, cmd: Dict[str, Any]) -> None:
        from services import node_ops

        try:
            value, ok = node_ops.run(cmd["op"], cmd.get("args") or {}), True
        except Exception as exc:  # noqa: BLE001 (reported to the master, not raised here)
            value, ok = f"{type(exc).__name__}: {exc}", False
        try:
            self._http("POST", "/api/nodes/results", {"id": cmd["id"], "ok": ok, "value": value})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not return result of %s: %s", cmd.get("op"), exc)

    def poll_once(self, wait: float = 25) -> List[Dict[str, Any]]:
        res = self._http("GET", f"/api/nodes/commands?wait={wait}&boot={self.boot}",
                         timeout=wait + 15)
        cmds = res.get("commands") or []
        for cmd in cmds:
            self.pool.submit(self._run_command, cmd)
        return cmds

    def _command_loop(self) -> None:
        while not self.stopping.is_set():
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Command poll failed: %s", exc)
                self.stopping.wait(5)

    def start(self) -> None:
        self.ensure_joined()
        for target, name in ((self._heartbeat_loop, "node-heartbeat"),
                             (self._command_loop, "node-commands")):
            threading.Thread(target=target, name=name, daemon=True).start()

    def stop(self) -> None:
        self.stopping.set()
        self.pool.shutdown(wait=False)


# ── what this machine has, and what it is doing ─────────────────────────────

def offered_gpus(gpus: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    raw = (settings.NODE_GPUS or "").strip()
    if not raw:
        return gpus
    allowed = {int(x) for x in raw.split(",") if x.strip().isdigit()}
    return [g for g in gpus if g.get("index") in allowed]


def _meminfo_mb() -> Dict[str, int]:
    out = {}
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, v = line.split(":")
                out[k] = int(v.split()[0]) // 1024
    except OSError:
        pass
    return out


def inventory() -> Dict[str, Any]:
    from services import gpu_monitor

    gpus = offered_gpus(gpu_monitor.get_gpu_status())
    return {
        "gpus": [{k: g.get(k) for k in ("index", "uuid", "name", "total_memory_mb")} for g in gpus],
        "cpu_count": os.cpu_count(),
        "mem_total_mb": _meminfo_mb().get("MemTotal"),
        "hostname": socket.gethostname(),
    }


def telemetry() -> Dict[str, Any]:
    """The heartbeat: GPUs, VRAM per job container, load and memory."""
    from services import container_manager, gpu_monitor, gpu_scan

    gpus = offered_gpus(gpu_monitor.get_gpu_status())
    gpu_scan.refresh()
    jobs: Dict[str, Any] = {}
    try:
        for c in container_manager.list_platform_containers(role="job"):
            job_id = (c.labels or {}).get(container_manager.LABEL_JOB)
            if job_id:
                jobs[job_id] = {"vram_mb": gpu_scan.container_mb(c.id), "status": c.status}
    except Exception as exc:  # noqa: BLE001 (docker away: report no jobs, not a crash)
        logger.debug("Job containers unavailable: %s", exc)
    mem = _meminfo_mb()
    return {
        "gpus": gpus,
        "gpu_telemetry_ok": gpu_monitor.telemetry()["ok"],
        "scan_usable": gpu_scan.usable(),
        "jobs": jobs,
        "load1": os.getloadavg()[0],
        "cpu_count": os.cpu_count(),
        "mem_available_mb": mem.get("MemAvailable"),
        "sent": time.time(),
    }


_agent: Optional[Agent] = None


def start() -> Agent:
    global _agent
    _agent = Agent()
    _agent.start()
    return _agent


def current() -> Optional[Agent]:
    return _agent
