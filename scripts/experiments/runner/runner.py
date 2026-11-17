#!/usr/bin/env python3
"""Run queued experiments on the shared host only while it is idle.

The host serves real users.  An experiment here is a guest: it starts only
when nobody needs the machine and leaves as soon as somebody does.

    runner.py tick        # what cron runs every 5 minutes
    runner.py gate        # print the idle check, change nothing
    runner.py status      # the queue and what happened to each entry
    runner.py add SPEC    # append an experiment (JSON file) to the queue
    runner.py reset ID    # put an aborted or failed entry back to pending

Idle means, for STABLE_TICKS consecutive ticks:

* no production batch job is waiting (queued or starting), and none is
  running or paused on a GPU the experiment needs (users in EXCLUDE_USERS, the
  experiment accounts, do not count): a real job must never wait because of an
  experiment, but one already running on another card does not mind it;
* every GPU the experiment needs shows at most GPU_UTIL_MAX % utilisation and
  has at least the free memory the experiment asks for;
* the 5-minute load average is below LOAD_MAX, MemAvailable above
  MEM_MIN_GIB and the disk above DISK_MIN_GIB, or above the entry's own
  ``min_disk_gib`` for one that writes little.

While an experiment runs, every CHECK_SECONDS the runner aborts it if a
production job is waiting or lands on one of its GPUs, or if a process that
is not the experiment's own becomes active on one of them.  Aborting kills the experiment's process
group and runs its cleanup; the entry goes back to pending until it has been
tried MAX_ATTEMPTS times.

An experiment is a JSON object::

    {"id": "e1-monitor-r01", "cmd": ["python3", "e1.py", "--mode", "monitor"],
     "cwd": "scripts/experiments", "gpus": [0], "min_free_mib": 30000,
     "timeout_min": 90, "cleanup": ["bash", "cleanup.sh"]}

``gpus`` empty means CPU only.  The command receives EXP_GPUS (comma list),
EXP_ID and EXP_OUT (a fresh directory for its results).
"""

import argparse
import fcntl
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]          # the worktree
HERE = Path(__file__).resolve().parent
STATE = HERE / "state"
QUEUE = STATE / "queue.json"
EVENTS = STATE / "events.jsonl"
LOCK = STATE / "runner.lock"
RESULTS = ROOT / "scripts" / "experiments" / "results" / "runs"

PROD_DB = Path(os.environ.get("PROD_DB", str(Path.home() / "workspace-gpu/data/gpu_platform.db")))
EXCLUDE_USERS = set(os.environ.get("EXCLUDE_USERS", ",".join(
    [f"exp{i:02d}" for i in range(1, 17)] + ["expa", "expb", "expc", "expw"])).split(","))
LOAD_MAX = float(os.environ.get("LOAD_MAX", 12))
MEM_MIN_GIB = float(os.environ.get("MEM_MIN_GIB", 16))
DISK_MIN_GIB = float(os.environ.get("DISK_MIN_GIB", 20))
GPU_UTIL_MAX = int(os.environ.get("GPU_UTIL_MAX", 5))
STABLE_TICKS = int(os.environ.get("STABLE_TICKS", 3))
CHECK_SECONDS = int(os.environ.get("CHECK_SECONDS", 30))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", 6))
# Containers the experiments create carry one of these label prefixes.
OWN_LABELS = ("gpu-platform-dev.", "gpu-platform-emu", "exp.runner")

_CID = re.compile(r"([0-9a-f]{64})")


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def log(event, **kw):
    STATE.mkdir(parents=True, exist_ok=True)
    with open(EVENTS, "a") as fh:
        fh.write(json.dumps({"t": now(), "event": event, **kw}) + "\n")


def load_queue():
    try:
        return json.loads(QUEUE.read_text())
    except FileNotFoundError:
        return {"entries": [], "idle_ticks": 0}


def save_queue(q):
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = QUEUE.with_suffix(".tmp")
    tmp.write_text(json.dumps(q, indent=1))
    tmp.replace(QUEUE)


# ── what the host is doing ───────────────────────────────────────────────────

def production_jobs():
    """Active production batch jobs of real users (read-only view of the DB)."""
    con = sqlite3.connect(f"file:{PROD_DB}?mode=ro", uri=True, timeout=10)
    try:
        rows = con.execute(
            "SELECT j.id, u.username, j.status, j.gpu_indices FROM jobs j "
            "JOIN users u ON u.id = j.user_id "
            "WHERE j.status IN ('queued','starting','running','paused')").fetchall()
    finally:
        con.close()
    return [r for r in rows if r[1] not in EXCLUDE_USERS]


def in_the_way(jobs, gpus):
    """The production jobs an experiment on *gpus* must give way to."""
    gpus = set(gpus or [])
    out = []
    for jid, user, status, indices in jobs:
        on = {int(x) for x in (indices or "").split(",") if x.strip().isdigit()}
        if status in ("queued", "starting") or (on & gpus):
            out.append((jid, user, status, indices))
    return out


def gpus():
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=index,uuid,utilization.gpu,memory.used,memory.total",
         "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=30).stdout
    cards = {}
    for line in out.splitlines():
        i, uuid, util, used, total = [p.strip() for p in line.split(",")]
        cards[int(i)] = {"uuid": uuid, "util": int(util), "used": int(used), "total": int(total)}
    return cards


def _labels(cid, cache={}):
    if cid not in cache:
        out = subprocess.run(["docker", "inspect", "-f", "{{json .Config.Labels}}", cid],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        cache[cid] = json.loads(out) if out and out != "null" else {}
    return cache[cid]


def _is_own(pid, own_pids):
    if pid in own_pids:
        return True
    try:
        m = _CID.search(Path(f"/proc/{pid}/cgroup").read_text())
    except OSError:
        return False
    if not m:
        return False
    return any(k.startswith(OWN_LABELS) for k in _labels(m.group(1)))


def foreign_activity(indices, own_pids=()):
    """SM % of processes on these GPUs that are not the experiment's own."""
    by_uuid = {c["uuid"]: i for i, c in gpus().items()}
    apps = subprocess.run(["nvidia-smi", "--query-compute-apps=gpu_uuid,pid", "--format=csv,noheader"],
                          capture_output=True, text=True, timeout=30).stdout
    sm = {}
    for line in subprocess.run(["nvidia-smi", "pmon", "-c", "1", "-s", "u"],
                               capture_output=True, text=True, timeout=30).stdout.splitlines():
        p = line.split()
        if len(p) >= 4 and p[1].isdigit():
            sm[int(p[1])] = int(p[3]) if p[3].isdigit() else 0
    busy = []
    for line in apps.splitlines():
        uuid, pid = [x.strip() for x in line.split(",")]
        if by_uuid.get(uuid) in indices and not _is_own(int(pid), set(own_pids)):
            if sm.get(int(pid), 0) > GPU_UTIL_MAX:
                busy.append({"gpu": by_uuid[uuid], "pid": int(pid), "sm": sm[int(pid)]})
    return busy


def host():
    load5 = os.getloadavg()[1]
    mem = {}
    for line in open("/proc/meminfo"):
        k, v = line.split(":")
        mem[k] = int(v.split()[0])
    st = os.statvfs(str(ROOT))
    return {"load5": round(load5, 2), "mem_avail_gib": round(mem["MemAvailable"] / 2**20, 1),
            "disk_free_gib": round(st.f_bavail * st.f_frsize / 2**30, 1)}


def gate(entry=None, all_gpus=False, disk_min=None):
    """``(ok, reasons, snapshot)`` for the host, and for this entry's GPUs.

    Without an entry only the host and production are checked (all_gpus adds
    every card, for the `gate` report): one busy card must not hold back an
    experiment that needs the other one, or none."""
    reasons = []
    h = host()
    if disk_min is None:
        disk_min = (entry or {}).get("min_disk_gib", DISK_MIN_GIB)
    if h["load5"] >= LOAD_MAX:
        reasons.append(f"load5 {h['load5']} >= {LOAD_MAX}")
    if h["mem_avail_gib"] < MEM_MIN_GIB:
        reasons.append(f"MemAvailable {h['mem_avail_gib']} GiB < {MEM_MIN_GIB}")
    if h["disk_free_gib"] < disk_min:
        reasons.append(f"disk {h['disk_free_gib']} GiB < {disk_min}")
    try:
        jobs = production_jobs()
    except sqlite3.Error as exc:
        jobs = None
        reasons.append(f"production DB unreadable: {exc}")
    blocking = in_the_way(jobs or [], (entry or {}).get("gpus", []))
    if blocking:
        reasons.append(f"{len(blocking)} production job(s) waiting or on these GPUs")
    cards = gpus()
    need = entry.get("gpus", []) if entry else (list(cards) if all_gpus else [])
    for i in need:
        c = cards.get(i)
        if c is None:
            reasons.append(f"GPU {i} missing")
            continue
        if c["util"] > GPU_UTIL_MAX:
            reasons.append(f"GPU {i} util {c['util']}%")
        free = c["total"] - c["used"]
        if entry and free < entry.get("min_free_mib", 0):
            reasons.append(f"GPU {i} free {free} MiB < {entry['min_free_mib']}")
    return not reasons, reasons, {"host": h, "gpus": cards, "prod_jobs": jobs}


# ── running one entry ────────────────────────────────────────────────────────

def _cleanup(entry):
    if entry.get("cleanup"):
        subprocess.run(entry["cleanup"], cwd=ROOT / entry.get("cwd", "."), timeout=600)


def run(entry):
    out = RESULTS / entry["id"] / datetime.now().strftime("%Y%m%dT%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, EXP_ID=entry["id"], EXP_OUT=str(out),
               EXP_GPUS=",".join(str(g) for g in entry.get("gpus", [])))
    with open(out / "stdout.log", "w") as so, open(out / "stderr.log", "w") as se:
        proc = subprocess.Popen(entry["cmd"], cwd=ROOT / entry.get("cwd", "."), env=env,
                                stdout=so, stderr=se, start_new_session=True)
    log("start", id=entry["id"], pid=proc.pid, out=str(out))
    deadline = time.time() + 60 * entry.get("timeout_min", 120)
    why = None
    while proc.poll() is None:
        time.sleep(CHECK_SECONDS)
        try:
            jobs = in_the_way(production_jobs(), entry.get("gpus", []))
        except sqlite3.Error:
            jobs = []
        if jobs:
            why = f"production job(s) {jobs}"
        elif entry.get("gpus"):
            own = _process_tree(proc.pid)
            busy = foreign_activity(set(entry["gpus"]), own)
            if busy:
                why = f"foreign GPU activity {busy}"
        if why is None and time.time() > deadline:
            why = "timeout"
        if why:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(30)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            break
    _cleanup(entry)
    if why:
        log("abort", id=entry["id"], reason=why, out=str(out))
        return ("aborted" if why != "timeout" else "failed"), why, str(out)
    status = "done" if proc.returncode == 0 else "failed"
    log("end", id=entry["id"], status=status, rc=proc.returncode, out=str(out))
    return status, f"rc={proc.returncode}", str(out)


def _process_tree(root):
    kids = {root}
    try:
        out = subprocess.run(["ps", "-e", "-o", "pid=,ppid="], capture_output=True, text=True).stdout
    except OSError:
        return kids
    pairs = [tuple(map(int, l.split())) for l in out.splitlines() if l.strip()]
    grew = True
    while grew:
        grew = False
        for pid, ppid in pairs:
            if ppid in kids and pid not in kids:
                kids.add(pid)
                grew = True
    return kids


# ── commands ─────────────────────────────────────────────────────────────────

def tick(_args):
    STATE.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return                                 # an experiment is running
        q = load_queue()
        pending = [e for e in q["entries"] if e.get("status", "pending") == "pending"]
        if not pending:
            return
        # the host check uses the most lenient disk floor among what is
        # pending; each entry is then held to its own
        ok, reasons, snap = gate(disk_min=min(e.get("min_disk_gib", DISK_MIN_GIB) for e in pending))
        q["idle_ticks"] = q.get("idle_ticks", 0) + 1 if ok else 0
        save_queue(q)
        if not ok:
            log("busy", reasons=reasons)
            return
        if q["idle_ticks"] < STABLE_TICKS:
            log("idle", ticks=q["idle_ticks"])
            return
        for entry in pending:
            ok, reasons, _ = gate(entry)
            if not ok:
                if entry.get("waiting") != reasons:
                    entry["waiting"] = reasons
                    save_queue(q)
                    log("waiting", id=entry["id"], reasons=reasons)
                continue
            entry.pop("waiting", None)
            entry["status"] = "running"
            entry["attempts"] = entry.get("attempts", 0) + 1
            save_queue(q)
            status, note, out = run(entry)
            q = load_queue()
            for e in q["entries"]:
                if e["id"] == entry["id"]:
                    e["attempts"] = entry["attempts"]
                    e.setdefault("history", []).append(
                        {"t": now(), "status": status, "note": note, "out": out})
                    if status == "aborted" and e["attempts"] < e.get("max_attempts", MAX_ATTEMPTS):
                        e["status"] = "pending"
                    else:
                        e["status"] = status
            q["idle_ticks"] = 0                    # re-check stability before the next one
            save_queue(q)
            return                                 # one experiment per tick


def cmd_gate(_args):
    ok, reasons, snap = gate(all_gpus=True)
    print(json.dumps({"ok": ok, "reasons": reasons, **snap}, indent=1, default=str))


def status(_args):
    q = load_queue()
    for e in q["entries"]:
        last = (e.get("history") or [{}])[-1]
        print(f"{e['id']:32} {e.get('status', 'pending'):8} attempts={e.get('attempts', 0)} "
              f"{last.get('t', '')} {last.get('note', '')}")
        if e.get("waiting"):
            print(f"{'':32} waiting: {'; '.join(e['waiting'])}")
    print(f"idle_ticks={q.get('idle_ticks', 0)}")


def add(args):
    spec = json.loads(Path(args.spec).read_text())
    specs = spec if isinstance(spec, list) else [spec]
    q = load_queue()
    have = {e["id"] for e in q["entries"]}
    for s in specs:
        if s["id"] in have:
            sys.exit(f"duplicate id {s['id']}")
        s["status"] = "pending"
        q["entries"].append(s)
    save_queue(q)


def reset(args):
    q = load_queue()
    for e in q["entries"]:
        if e["id"] == args.id:
            e["status"] = "pending"
            e["attempts"] = 0
    save_queue(q)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tick")
    sub.add_parser("gate")
    sub.add_parser("status")
    a = sub.add_parser("add")
    a.add_argument("spec")
    r = sub.add_parser("reset")
    r.add_argument("id")
    args = ap.parse_args()
    {"tick": tick, "gate": cmd_gate, "status": status, "add": add, "reset": reset}[args.cmd](args)


if __name__ == "__main__":
    main()
