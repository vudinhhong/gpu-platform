#!/usr/bin/env python3
"""E2: the control plane with 1 to 16 accounts using it at the same time.

The dev stack, no nodes.  At each level N, accounts exp01..expNN

  1. start their workspaces at the same moment: time until the start call
     returns, and until JupyterLab answers through the proxy;
  2. for LOAD seconds, each submit a CPU job (sleep JOB_S) every PERIOD
     seconds and poll their job list and the shared queue every 2 s, as an
     open dashboard does: latency of every request;
  3. cancel what is left and stop the workspaces.

Over phase 2 the backend's CPU time and memory are read from its cgroup, and
the scheduler's loop times from its log (LOOP_TIMING_LOG=true).  Jobs are CPU
only, so the experiment needs no GPU; the scheduler still does its full pass,
including the nvidia-smi query.

    EXP_OUT=out python3 scripts/experiments/e2_users.py --levels 1,2,4,8,16 --reps 3
"""

import argparse
import json
import math
import os
import re
import statistics as st
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import e1_vram as e1  # noqa: E402  (sets the dev stack defaults)
import platform_api as api  # noqa: E402

PASSWORD = api.experiment_password()
MASTER = "gpu-platform-dev-backend-1"

_CLIENT = r'''
import asyncio, json, sys, time
import httpx
s = json.load(sys.stdin)
BASE = "http://127.0.0.1:8000"

def H(tok):
    return {"Authorization": "Bearer " + tok}

async def start(c, user, tok):
    t0 = time.perf_counter()
    r = await c.post(BASE + "/api/user/me/jupyter/start", headers=H(tok), json={}, timeout=600)
    t_call = time.perf_counter() - t0
    ok = r.status_code in (200, 201)
    t_ready = None
    while ok and time.perf_counter() - t0 < 300:
        try:
            p = await c.get(BASE + f"/jupyter/{user}/api/status", headers=H(tok), timeout=10)
            if p.status_code == 200:
                t_ready = time.perf_counter() - t0
                break
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.2)
    return {"user": user, "status": r.status_code, "call_s": round(t_call, 3),
            "ready_s": round(t_ready, 3) if t_ready else None}

async def user_load(c, user, tok, end, rec, jobs):
    next_submit = time.perf_counter()
    while time.perf_counter() < end:
        now = time.perf_counter()
        if now >= next_submit:
            next_submit += s["period"]
            t0 = time.perf_counter()
            try:
                r = await c.post(BASE + "/api/jobs", headers=H(tok), timeout=60,
                                 json={"script": "e2/job.sh", "name": "e2"})
                rec.append(("submit", (time.perf_counter() - t0) * 1000, r.status_code))
                if r.status_code == 201:
                    jobs.append((user, r.json()["id"]))
            except httpx.HTTPError:
                # 599: no answer within the client's 60 s, the request counts as failed
                rec.append(("submit", (time.perf_counter() - t0) * 1000, 599))
        for kind, path in (("list", "/api/jobs?active_only=true"), ("queue", "/api/jobs/queue")):
            t0 = time.perf_counter()
            try:
                r = await c.get(BASE + path, headers=H(tok), timeout=60)
                code = r.status_code
            except httpx.HTTPError:
                code = 599
            rec.append((kind, (time.perf_counter() - t0) * 1000, code))
        await asyncio.sleep(2)

async def main():
    limits = httpx.Limits(max_connections=200, max_keepalive_connections=200)
    async with httpx.AsyncClient(limits=limits) as c:
        if s["phase"] == "start":
            res = await asyncio.gather(*(start(c, u, t) for u, t in s["tokens"].items()))
            print(json.dumps(res))
            return
        rec, jobs = [], []
        end = time.perf_counter() + s["seconds"]
        await asyncio.gather(*(user_load(c, u, t, end, rec, jobs) for u, t in s["tokens"].items()))
        waits = []
        for user, jid in jobs:
            r = await c.get(BASE + f"/api/jobs/{jid}", headers=H(s["tokens"][user]), timeout=300)
            j = r.json()
            waits.append({"job": jid, "status": j.get("status"), "waited_s": j.get("waited_seconds")})
        by_user = {}
        for user, jid in jobs:
            by_user.setdefault(user, []).append(jid)
        for user, ids in by_user.items():
            await c.post(BASE + "/api/jobs/cancel", headers=H(s["tokens"][user]), json={"ids": ids},
                         timeout=300)
        print(json.dumps({"requests": rec, "jobs": waits}))

asyncio.run(main())
'''


def in_master(spec, timeout=1800):
    res = subprocess.run(["docker", "exec", "-i", MASTER, "python", "-c", _CLIENT],
                         input=json.dumps(spec), capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        raise SystemExit(f"client in the master failed: {res.stderr[-2000:]}")
    return json.loads(res.stdout.strip().splitlines()[-1])


def users(n):
    return [f"exp{i:02d}" for i in range(1, n + 1)]


def cgroup_cpu_usec():
    stat = api.read_cgroup(MASTER, "cpu.stat")
    return int(re.search(r"usage_usec (\d+)", stat).group(1))


def cgroup_mem_mib():
    return int(api.read_cgroup(MASTER, "memory.current")) / 2**20


def loop_times(since):
    logs = subprocess.run(["docker", "logs", "--since", since, MASTER],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True).stdout
    out = {}
    for name, ms in re.findall(r"loop (\S+) took ([\d.]+) ms", logs):
        out.setdefault(name, []).append(float(ms))
    for m in re.finditer(r"pass finalise ([\d.]+) resume ([\d.]+) dispatch ([\d.]+) ms, (\d+) started", logs):
        for key, value in zip(("finalise", "resume", "dispatch", "started"), m.groups()):
            out.setdefault("pass-" + key, []).append(float(value))
    return out


def summary(xs):
    xs = sorted(xs)
    if not xs:
        return None
    return {"n": len(xs), "median": round(st.median(xs), 2),
            "p95": round(xs[math.ceil(0.95 * len(xs)) - 1], 2), "max": round(xs[-1], 2),
            "mean": round(st.mean(xs), 2)}


def stop_all(tokens):
    for tok in tokens.values():
        api.stop_workspace(tok)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--levels", default="1,2,4,8,16")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--load", type=int, default=120, help="seconds of load per repetition")
    ap.add_argument("--period", type=float, default=10.0, help="seconds between a user's jobs")
    ap.add_argument("--job-seconds", type=int, default=20)
    args = ap.parse_args()
    levels = [int(x) for x in args.levels.split(",")]
    out = Path(os.environ.get("EXP_OUT") or ".")
    out.mkdir(parents=True, exist_ok=True)
    done_file = out.parent / "e2_done.jsonl"      # shared across attempts
    done = set()
    if done_file.exists():
        done = {(r["n"], r["rep"]) for r in map(json.loads, done_file.read_text().splitlines())}

    admin = api.admin_token()
    e1.only_the_master(admin)
    e1.restart_backend(dict(os.environ, LOOP_TIMING_LOG="true", JOB_GPU_POOL="-1",
                            JOB_MAX_RUNNING_PER_USER="2"))
    admin = api.admin_token()
    tokens = {}
    for u in users(max(levels)):
        api.ensure_user(admin, u, PASSWORD, gpu_indices=[], memory_limit_mb=2048,
                        cpu_cores=0.5, max_processes=512)
        tokens[u] = api.login(u, PASSWORD)
    stop_all(tokens)
    try:
        for n in levels:
            for rep in range(args.reps):
                if (n, rep) in done:
                    continue
                toks = {u: tokens[u] for u in users(n)}
                time.sleep(5)
                starts = in_master({"phase": "start", "tokens": toks}, timeout=900)
                for u in toks:
                    api.sh(f"{e1.PREFIX}jupyter-{u}", u,
                           f"mkdir -p ~/e2 && printf 'sleep {args.job_seconds}\\n' > ~/e2/job.sh")
                since = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                cpu0, t0 = cgroup_cpu_usec(), time.time()
                mem = [cgroup_mem_mib()]
                load = in_master({"phase": "load", "tokens": toks, "seconds": args.load,
                                  "period": args.period}, timeout=args.load + 900)
                cpu1, t1 = cgroup_cpu_usec(), time.time()
                mem.append(cgroup_mem_mib())
                loops = loop_times(since)
                reqs = load["requests"]
                errors = sum(1 for _, _, code in reqs if code >= 400 and code != 429)
                row = {
                    "n": n, "rep": rep, "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "start_call_s": summary([s["call_s"] for s in starts]),
                    "start_ready_s": summary([s["ready_s"] for s in starts if s["ready_s"]]),
                    "start_failed": sum(1 for s in starts if not s["ready_s"]),
                    "api_ms": {k: summary([ms for kind, ms, _ in reqs if kind == k])
                               for k in ("submit", "list", "queue")},
                    "requests": len(reqs), "request_errors": errors,
                    "refused_429": sum(1 for _, _, code in reqs if code == 429),
                    "timeouts": sum(1 for _, _, code in reqs if code == 599),
                    "jobs_submitted": len(load["jobs"]),
                    "job_wait_s": summary([j["waited_s"] for j in load["jobs"]
                                           if j["waited_s"] is not None]),
                    "backend_cpu_cores": round((cpu1 - cpu0) / 1e6 / (t1 - t0), 3),
                    "backend_mem_mib": round(max(mem), 1),
                    "scheduler_ms": summary(loops.get("job-scheduler", [])),
                    "guard_ms": summary(loops.get("gpu-guard", [])),
                    "pass_ms": {k: summary(loops.get("pass-" + k, []))
                                for k in ("finalise", "resume", "dispatch")},
                    "started_per_pass": summary(loops.get("pass-started", [])),
                }
                print(json.dumps(row), flush=True)
                with open(out / "e2_levels.jsonl", "a") as fh:
                    fh.write(json.dumps(row) + "\n")
                with open(out / f"e2_raw_{n}_r{rep}.json", "w") as fh:
                    json.dump({"starts": starts, **load}, fh)
                with open(done_file, "a") as fh:
                    fh.write(json.dumps({"n": n, "rep": rep, "out": str(out)}) + "\n")
                stop_all(toks)
    finally:
        stop_all(tokens)
        e1.restart_backend()


if __name__ == "__main__":
    main()
