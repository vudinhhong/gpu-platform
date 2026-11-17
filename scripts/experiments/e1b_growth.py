#!/usr/bin/env python3
"""E1b: a neighbour that has not yet grown to what it asked for.

E1's neighbour took its whole request at the start, so an overrun could only
reach the slack beyond both requests.  Real jobs grow: a training run asks
for R, holds half of it while loading, and reaches R later.  Here the
neighbour B asks for R = 45% of the free memory and holds R/2; the offender A
asks for R, holds it, and then takes OVER = 25% of the free memory more.
Physically that leaves B too little to reach R, although B never goes past
its own request.  DELAY seconds after A's overrun began, B takes the other
R/2 in one allocation; the trial records whether that allocation hit a CUDA
out-of-memory error.

  monitor   the after-the-fact check: A keeps the memory until it is stopped,
            so B fails whenever it grows inside that window
  hami      HAMi-core: A is refused at its allowance, so B's half is still there

    EXP_GPUS=0 EXP_OUT=out python3 scripts/experiments/e1b_growth.py --modes monitor,hami
"""

import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path

import e1_vram as e1
from e1_vram import api

GROWER = r'''
import argparse, json, os, time, torch
ap = argparse.ArgumentParser()
ap.add_argument("--hold", type=int); ap.add_argument("--grow", type=int)
ap.add_argument("--watch"); ap.add_argument("--delay", type=float)
ap.add_argument("--seconds", type=float, default=60); ap.add_argument("--log")
a = ap.parse_args()
MiB = 2**20
log = open(os.path.expanduser(a.log), "a", buffering=1)
def rec(**kw): log.write(json.dumps({"t": time.time(), **kw}) + "\n")
def take(mib):
    t = torch.empty(mib * MiB, dtype=torch.uint8, device="cuda"); torch.cuda.synchronize(); return t
held = [take(512) for _ in range(a.hold // 512)]
if a.hold % 512: held.append(take(a.hold % 512))
rec(event="base", mib=a.hold)
watch = os.path.expanduser(a.watch)
end = time.time() + a.seconds
start = None
while start is None and time.time() < end:
    try:
        for line in open(watch):
            e = json.loads(line)
            if e["event"] == "over_start":
                start = e["t"]
    except (FileNotFoundError, ValueError):
        pass
    time.sleep(0.01)
if start is None:
    rec(event="no_overrun"); raise SystemExit(0)
time.sleep(max(0.0, start + a.delay - time.time()))
try:
    held.append(take(a.grow)); rec(event="grow_ok", after=time.time() - start)
except torch.OutOfMemoryError as err:
    rec(event="grow_oom", after=time.time() - start, msg=str(err).splitlines()[0][:200])
x = torch.randn(2048, 2048, device="cuda")
while time.time() < end:
    y = x @ x; torch.cuda.synchronize(); time.sleep(0.05)
rec(event="end")
'''


def setup_grower():
    user = e1.USERS["neighbour"]
    api.sh(f"{e1.PREFIX}jupyter-{user}", user,
           "mkdir -p ~/e1 && cat > ~/e1/g.py <<'PY'\n" + GROWER + "\nPY")


def submit(role, cmd, mib, trial):
    user = e1.USERS[role]
    script = f"e1/{trial}-{role}.sh"
    api.sh(f"{e1.PREFIX}jupyter-{user}", user, f"cat > ~/{script} <<'SH'\n{cmd}\nSH")
    res = api.sh(f"{e1.PREFIX}jupyter-{user}", user,
                 f"cd ~ && submit {script} --gpu-memory {mib} --name e1b-{role}")
    return int(res.stdout.split("job ")[1].split()[0])


def wait_done(job_id, token):
    while True:
        _, j = api._request("GET", f"/api/jobs/{job_id}", token)
        if j["status"] in ("succeeded", "failed", "cancelled", "timeout"):
            return j
        time.sleep(2)


def trial(tokens, gpu, mode, pattern, delay, rep):
    name = f"e1b-{mode}-{pattern}-d{delay}-r{rep}-{int(time.time())}"
    free = e1.free_mib(gpu)
    req = int(free * 0.45) // 256 * 256
    hold = req // 2 // 256 * 256
    over = int(free * 0.25) // 256 * 256
    user = e1.USERS["neighbour"]
    b_log, a_log = f"e1/{name}-neighbour.jsonl", f"e1/{name}-offender.jsonl"
    b_id = submit("neighbour", f"python3 ~/e1/g.py --hold {hold} --grow {req - hold} "
                  f"--watch ~/{a_log} --delay {delay} --seconds 60 --log ~/{b_log}", req, name)
    for _ in range(120):
        if any(x["event"] == "base" for x in e1.events(e1.HOME_DATA / user / b_log)):
            break
        time.sleep(1)
    a_id = submit("offender", f"python3 ~/e1/w.py --role offender --base {req} --over {over} "
                  f"--pattern {pattern} --seconds 40 --log ~/{a_log}", req, name)
    b, a = wait_done(b_id, tokens["neighbour"]), wait_done(a_id, tokens["offender"])
    be = e1.events(e1.HOME_DATA / user / b_log)
    ae = e1.events(e1.HOME_DATA / e1.USERS["offender"] / a_log)
    grow = next((x for x in be if x["event"] in ("grow_ok", "grow_oom", "no_overrun")), None)
    start = next((x["t"] for x in ae if x["event"] == "over_start"), None)
    refused = next((x for x in ae if x["event"] == "refused"), None)
    last = max((x["t"] for x in ae if x["event"] in ("beat", "step", "over_done")), default=None)
    survived = any(x["event"] == "end" for x in ae)
    return {
        "trial": name, "mode": mode, "pattern": pattern, "delay_s": delay, "rep": rep, "gpu": gpu,
        "free_mib": free, "request_mib": req, "hold_mib": hold, "over_mib": over,
        "neighbour": {"status": b["status"], "grow": grow and grow["event"],
                      "grow_after_s": grow and grow.get("after") and round(grow["after"], 3),
                      "message": b.get("message")},
        "offender": {"status": a["status"], "message": a.get("message"),
                     "refused_after_s": round(refused["t"] - start, 3) if refused and start else None,
                     "refused_at_mib": refused["got"] if refused else None,
                     "stopped_after_s": (round(last - start, 3)
                                         if (not survived and last and start) else None),
                     "survived": survived},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--modes", default="monitor,hami")
    ap.add_argument("--patterns", default="burst,ramp")
    ap.add_argument("--delays", default="0,0.5,1,2,5")
    ap.add_argument("--reps", type=int, default=3)
    args = ap.parse_args()
    gpu = int((os.environ.get("EXP_GPUS") or "0").split(",")[0])
    out = Path(os.environ.get("EXP_OUT") or ".")

    admin = api.admin_token()
    e1.only_the_master(admin)
    e1.setup(admin)
    setup_grower()
    # Attempts the runner aborted leave their trials beside this directory.
    done = set()
    for f in out.parent.glob("*/e1b_trials.jsonl"):
        for line in f.read_text().splitlines():
            r = json.loads(line)
            done.add((r["mode"], r["pattern"], r["delay_s"], r["rep"]))
    n = 0
    for mode in args.modes.split(","):
        e1.set_mode(mode, gpu)
        tokens = {r: api.login(u, e1.PASSWORD) for r, u in e1.USERS.items()}
        for rep in range(args.reps):
            for delay in [float(x) for x in args.delays.split(",")]:
                for pattern in args.patterns.split(","):
                    if (mode, pattern, delay, rep) in done:
                        continue
                    r = trial(tokens, gpu, mode, pattern, delay, rep)
                    n += 1
                    print(json.dumps(r), flush=True)
                    with open(out / "e1b_trials.jsonl", "a") as fh:
                        fh.write(json.dumps(r) + "\n")
    e1.restart_backend()
    for user in set(e1.USERS.values()):
        api.stop_workspace(api.login(user, e1.PASSWORD))
    (out / "e1b_summary.json").write_text(json.dumps(
        {"measured": datetime.now().astimezone().isoformat(timespec="seconds"),
         "gpu": gpu, "trials": n}, indent=1))


if __name__ == "__main__":
    main()
