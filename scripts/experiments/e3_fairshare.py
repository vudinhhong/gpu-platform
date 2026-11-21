#!/usr/bin/env python3
"""E3: the synthetic fair-share workload, run for real at 1/60 of the time.

The workload has the shape of the simulation's (fairshare_sim.synthetic):
user 1 submits a burst of 20 jobs at the start and another halfway through,
users 2..N submit one now and then.  Here one simulated hour is one minute,
and every job is a GPU job that only sleeps: what is measured is the order in
which the queue starts jobs, not what they compute.  Each job declares a GPU
memory reservation and the queue packs them onto one card by it.

  platform   the dev stack, one card (JOB_GPU_POOL), a pass every second,
             at most 2 running jobs per user, fair-share window, half-life
             and lookahead scaled by 1/60.  --policy fifo turns the score off
             (no window, no lookahead: every user scores 0, ties go by
             submission time); --policy fairshare is the platform's own.
  slurm      Slurm 21.08 in a container (scripts/experiments/slurm/), with
             slurmdbd so fair share and per-user limits work: multifactor
             priority with only the fair-share factor (and a tiny age factor
             for ties), half-life 6 min, MaxJobs=2 per user, one node whose
             memory is the card's, each job asking --mem equal to the
             platform's allowance for its reservation and a time limit one
             minute above its real duration, so backfill has what it needs.

Both write one row per job: user, submission offset, wait, duration.

    EXP_GPUS=0 EXP_OUT=out python3 scripts/experiments/e3_fairshare.py platform --policy fifo --seed 1
    EXP_OUT=out python3 scripts/experiments/e3_fairshare.py slurm --seed 1
"""

import argparse
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import e1_vram as e1  # noqa: E402  (sets the dev stack defaults)
import platform_api as api  # noqa: E402
from e3_workload import SCALE, workload  # noqa: E402

PASSWORD = api.experiment_password()
MASTER = "gpu-platform-dev-backend-1"
SLURM_IMAGE = "gpu-exp-slurm"

# Platform settings scaled by 1/SCALE (24 h window, 6 h half-life, 600 s
# lookahead).  The half-life cannot go below 0.1 h in the platform, which is
# 6 h / 60 exactly.
POLICIES = {
    "fairshare": {"FAIRSHARE_WINDOW_HOURS": "0.4", "FAIRSHARE_HALFLIFE_HOURS": "0.1",
                  "FAIRSHARE_LOOKAHEAD_SECONDS": "10"},
    "fifo":      {"FAIRSHARE_WINDOW_HOURS": "0", "FAIRSHARE_HALFLIFE_HOURS": "0.1",
                  "FAIRSHARE_LOOKAHEAD_SECONDS": "0"},
}


def allowance(req):
    return int(max(req * 1.1, req + 1024))


# --------------------------------------------------------------------------
# platform

_SUBMIT = r'''
import asyncio, json, sys, time
import httpx
s = json.load(sys.stdin)
BASE = "http://127.0.0.1:8000"

async def main():
    async with httpx.AsyncClient(timeout=60) as c:
        t0 = time.time()
        ids = {}
        async def one(j):
            await asyncio.sleep(max(0, t0 + j["submit"] - time.time()))
            tok = s["tokens"][str(j["user"])]
            body = {"script": f"e3/{s['tag']}/{j['k']}.sh", "name": f"e3-{j['k']}"}
            if not s["cpu_only"]:
                body["gpu_memory_mb"] = j["request_mb"]
            r = await c.post(BASE + "/api/jobs", headers={"Authorization": "Bearer " + tok}, json=body)
            ids[j["k"]] = (r.status_code, r.json().get("id") if r.status_code == 201 else r.text[:200],
                           round(time.time() - t0, 2))
        await asyncio.gather(*(one(j) for j in s["jobs"]))
        rows = []
        pending = {k: v for k, v in ids.items() if v[0] == 201}
        final = {}
        while pending and time.time() - t0 < s["deadline"]:
            await asyncio.sleep(3)
            for k, (code, jid, sub) in list(pending.items()):
                user = str(s["jobs"][k]["user"])
                r = await c.get(BASE + f"/api/jobs/{jid}", headers={"Authorization": "Bearer " + s["tokens"][user]})
                j = r.json()
                if j["status"] in ("succeeded", "failed", "cancelled", "timeout"):
                    final[k] = j
                    del pending[k]
        for j in s["jobs"]:
            code, jid, sub = ids[j["k"]]
            v = final.get(j["k"], {})
            rows.append({**j, "submitted_at": sub, "http": code, "job": jid,
                         "status": v.get("status", "unfinished"),
                         "wait": v.get("waited_seconds"), "runtime": v.get("runtime_seconds"),
                         "gpus": v.get("gpus")})
        print(json.dumps(rows))

asyncio.run(main())
'''


def run_platform(args, jobs, out):
    gpu = int((os.environ.get("EXP_GPUS") or "0").split(",")[0])
    admin = api.admin_token()
    e1.only_the_master(admin)
    tag = f"s{args.seed}-{args.policy}"
    users = sorted({j["user"] for j in jobs})
    # Fresh accounts per run: the fair-share history of one run must not
    # carry into the next.
    # Accounts of earlier runs keep their SSH ports until deactivated, and
    # the dev range is small: once it is used up, workspaces start without
    # SSH.  None of them is needed again.
    status, listed = api._request("GET", "/api/admin/users", admin)
    for u in listed if status == 200 else []:
        if u["username"].startswith("e3") and u.get("is_active"):
            api.deactivate(admin, u["username"])
    # A repeated run gets new names too, from the minute it started.
    run = time.strftime("%H%M")
    names = {u: f"e3{args.policy[:2]}{args.seed}r{run}u{u:02d}" for u in users}
    tokens = {}
    for u, name in names.items():
        api.ensure_user(admin, name, PASSWORD, gpu_indices=[], memory_limit_mb=1024,
                        cpu_cores=0.5, max_processes=256)
        tok = api.login(name, PASSWORD)
        api.start_workspace(tok)
        cont = f"{e1.PREFIX}jupyter-{name}"
        api.wait_running(cont)
        mine = [j for j in jobs if j["user"] == u]
        lines = "".join(f"printf 'sleep {j['duration']}\\n' > ~/e3/{tag}/{j['k']}.sh\n"
                        for j in mine)
        # The exec can fail in the moment after the workspace comes up, and
        # a missing script only shows later as a refused submission.
        for _ in range(10):
            res = api.sh(cont, name, f"mkdir -p ~/e3/{tag}\n{lines}ls ~/e3/{tag} | wc -l")
            if res.returncode == 0 and res.stdout.strip().splitlines()[-1:] == [str(len(mine))]:
                break
            time.sleep(3)
        else:
            raise SystemExit(f"could not write the job scripts of {name}: {res.stderr[-500:]}")
        api.stop_workspace(tok)
        tokens[str(u)] = tok
    e1.restart_backend(dict(os.environ, JOB_GPU_POOL=str(gpu), JOB_SCHEDULER_INTERVAL="1",
                            JOB_MAX_RUNNING_PER_USER="2", GPU_HARD_VRAM_LIMIT="false",
                            **POLICIES[args.policy]))
    free = e1.free_mib(gpu)
    deadline = max(j["submit"] for j in jobs) + sum(j["duration"] for j in jobs) + 600
    res = subprocess.run(["docker", "exec", "-i", MASTER, "python", "-c", _SUBMIT],
                         input=json.dumps({"tokens": tokens, "jobs": jobs, "tag": tag,
                                           "deadline": deadline, "cpu_only": args.cpu_only}),
                         capture_output=True, text=True, timeout=deadline + 300)
    if res.returncode != 0:
        raise SystemExit(f"submit client failed: {res.stderr[-2000:]}")
    rows = json.loads(res.stdout.strip().splitlines()[-1])
    refused = [r for r in rows if r["http"] != 201]
    if refused:
        raise SystemExit(f"{len(refused)} submissions refused, e.g. {refused[0]['job']}")
    return {"system": "platform", "policy": args.policy, "gpu": gpu,
            "free_mib_at_start": free, "users": names}, rows


# --------------------------------------------------------------------------
# slurm

_SBATCH = r'''
import json, subprocess, sys, threading, time
s = json.load(sys.stdin)
t0 = time.time()
ids = {}
def one(j):
    time.sleep(max(0, t0 + j["submit"] - time.time()))
    cmd = (f"cd /tmp && sbatch --parsable -J e3-{j['k']} -c1 --mem={j['mem']} "
           f"--time={j['limit_min']} -o /dev/null --wrap='sleep {j['duration']}'")
    r = subprocess.run(["su", f"u{j['user']}", "-c", cmd], capture_output=True, text=True)
    ids[j["k"]] = (r.stdout.strip(), round(time.time() - t0, 2), r.stderr.strip()[:200])
ts = [threading.Thread(target=one, args=(j,)) for j in s["jobs"]]
for t in ts: t.start()
for t in ts: t.join()
while time.time() - t0 < s["deadline"]:
    q = subprocess.run(["squeue", "-h", "-o", "%i"], capture_output=True, text=True).stdout.split()
    if not q:
        break
    time.sleep(3)
acct = subprocess.run(["sacct", "-X", "-n", "-P", "-a", "-o", "JobID,User,Submit,Start,End,State",
                       "--starttime", "now-1days"], capture_output=True, text=True).stdout
print(json.dumps({"ids": ids, "sacct": acct}))
'''


def run_slurm(args, jobs, out):
    name = f"e3-slurm-s{args.seed}"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    subprocess.run(["docker", "build", "-q", "-t", SLURM_IMAGE,
                    os.path.join(os.path.dirname(os.path.abspath(__file__)), "slurm")],
                   check=True, capture_output=True)
    # Slurm 23.11 sets up a cgroup tree for its step daemons even when it
    # confines nothing, so the container needs its own writable one.
    subprocess.run(["docker", "run", "-d", "--name", name, "--hostname", "localhost",
                    "--privileged", "--cgroupns=private",
                    "--label", "exp.runner=e3", "-e", f"E3_MEM_MIB={args.mem_mib}",
                    "-e", "E3_MAX_RUNNING=2", SLURM_IMAGE], check=True, capture_output=True)
    try:
        for _ in range(90):
            if subprocess.run(["docker", "exec", name, "test", "-f", "/ready"]).returncode == 0:
                break
            time.sleep(2)
        else:
            raise SystemExit("slurm did not come up")
        spec = [{**j, "mem": allowance(j["request_mb"]),
                 "limit_min": math.ceil(j["duration"] / 60) + 1} for j in jobs]
        deadline = max(j["submit"] for j in jobs) + sum(j["duration"] for j in jobs) + 600
        res = subprocess.run(["docker", "exec", "-i", name, "python3", "-c", _SBATCH],
                             input=json.dumps({"jobs": spec, "deadline": deadline}),
                             capture_output=True, text=True, timeout=deadline + 300)
        if res.returncode != 0:
            raise SystemExit(f"slurm client failed (rc {res.returncode}): {res.stderr[-2000:]} {res.stdout[-500:]}")
        got = json.loads(res.stdout.strip().splitlines()[-1])
        acct = {}
        for line in got["sacct"].splitlines():
            jid, user, sub, start, end, state = line.split("|")
            acct[jid] = (sub, start, end, state)
        from datetime import datetime as dt

        def ts(x):
            try:
                return dt.fromisoformat(x).timestamp()
            except ValueError:
                return None
        rows = []
        for j in jobs:
            jid, sub_at, err = got["ids"][str(j["k"])]
            sub, start, end, state = acct.get(jid, (None, None, None, "missing"))
            wait = (ts(start) - ts(sub)) if sub and ts(start) and ts(sub) else None
            rows.append({**j, "submitted_at": sub_at, "job": jid, "error": err or None,
                         "status": state.lower(), "wait": wait,
                         "runtime": (ts(end) - ts(start)) if ts(end) and ts(start) else None})
        conf = subprocess.run(["docker", "exec", name, "scontrol", "show", "config"],
                              capture_output=True, text=True).stdout
        return {"system": "slurm", "policy": "multifactor", "mem_mib": args.mem_mib,
                "version": next((l.split("=")[1].strip() for l in conf.splitlines()
                                 if l.startswith("SLURM_VERSION")), None)}, rows
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("system", choices=["platform", "slurm"])
    ap.add_argument("--policy", choices=list(POLICIES), default="fairshare")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--users", type=int, default=8)
    ap.add_argument("--hours", type=float, default=24.0, help="simulated hours")
    ap.add_argument("--cpu-only", action="store_true",
                    help="platform: submit without a GPU reservation (a test of the harness)")
    ap.add_argument("--mem-mib", type=int, default=49140 - 512,
                    help="slurm: the card's memory less the platform's headroom")
    args = ap.parse_args()
    out = Path(os.environ.get("EXP_OUT") or ".")
    out.mkdir(parents=True, exist_ok=True)
    jobs = workload(args.seed, args.users, args.hours)
    t0 = time.time()
    try:
        meta, rows = (run_platform if args.system == "platform" else run_slurm)(args, jobs, out)
    finally:
        if args.system == "platform":
            e1.restart_backend()
    meta.update({"seed": args.seed, "users": args.users, "hours": args.hours, "scale": SCALE,
                 "jobs": len(jobs), "started": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t0)),
                 "elapsed_s": round(time.time() - t0)})
    with open(out / "e3_run.json", "w") as fh:
        json.dump({"meta": meta, "jobs": rows}, fh, indent=1)
    waits = [r["wait"] for r in rows if r["wait"] is not None]
    print(json.dumps({**meta, "finished": len(waits),
                      "mean_wait_s": round(sum(waits) / max(1, len(waits)), 1)}), flush=True)
    if len(waits) < len(rows):
        raise SystemExit(f"{len(rows) - len(waits)} of {len(rows)} jobs did not finish")


if __name__ == "__main__":
    main()
