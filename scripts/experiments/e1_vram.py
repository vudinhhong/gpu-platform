#!/usr/bin/env python3
"""E1: what happens to a neighbour when a job takes more GPU memory than it asked for.

Two experiment accounts share one card through the ordinary queue (the dev
stack's JOB_GPU_POOL holds it to the card the runner hands over).  The
neighbour asks for 40% of the free memory, holds it, computes, and every
100 ms takes and releases a 1 GiB transient through cudaMalloc, as a training
step's activations do.  The offender asks for another 40%, holds it, and then
takes OVER more, all at once (burst) or 256 MiB every 50 ms (ramp).  Both
requests fit side by side; the overrun does not.

Three enforcement modes, set on the dev backend between blocks:

  monitor   the platform's after-the-fact check (JOB_GPU_OVERRUN_ACTION=stop)
  hami      HAMi-core only (GPU_HARD_VRAM_LIMIT, the check set to warn)
  both      HAMi-core with the check still on (the shipped configuration)

Per trial: whether the neighbour saw a CUDA out-of-memory error, how long
after the overrun began the offender was stopped or refused, and how far past
its allowance it got.

    EXP_GPUS=1 EXP_OUT=out python3 scripts/experiments/e1_vram.py --modes monitor --reps 5
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("PLATFORM_URL", "http://127.0.0.1:18001")
os.environ.setdefault("ADMIN_PASSWORD_FILE", "~/.gpu_dev_admin_pw")
# Until the firewall rule for the dev bridge exists, talk to it from inside.
os.environ.setdefault("PLATFORM_EXEC", "gpu-platform-dev-backend-1")
import platform_api as api  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.gpu.yml",
           "-f", "docker-compose.host.yml"]
PREFIX = "gpu-dev-"
# One account submits both jobs: the limit is per job, and a second account
# would only need its own 4.5 GB PyTorch install.
USERS = {"neighbour": "exp01", "offender": "exp01"}
PASSWORD = "Experiment-pass-1"
HOME_DATA = ROOT / "jupyter_data"
MODES = {
    "monitor": {"GPU_HARD_VRAM_LIMIT": "false", "JOB_GPU_OVERRUN_ACTION": "stop"},
    "hami":    {"GPU_HARD_VRAM_LIMIT": "true",  "JOB_GPU_OVERRUN_ACTION": "warn"},
    "both":    {"GPU_HARD_VRAM_LIMIT": "true",  "JOB_GPU_OVERRUN_ACTION": "stop"},
}

WORKLOAD = r'''
import argparse, json, os, time, torch
ap = argparse.ArgumentParser()
ap.add_argument("--role"); ap.add_argument("--base", type=int); ap.add_argument("--over", type=int, default=0)
ap.add_argument("--pattern", default="burst"); ap.add_argument("--seconds", type=float, default=90)
ap.add_argument("--log")
a = ap.parse_args()
MiB = 2**20
log = open(a.log, "a", buffering=1)
def rec(**kw): log.write(json.dumps({"t": time.time(), **kw}) + "\n")
held = []
def take(mib):
    t = torch.empty(mib * MiB, dtype=torch.uint8, device="cuda"); torch.cuda.synchronize(); return t
for _ in range(a.base // 512): held.append(take(512))
if a.base % 512: held.append(take(a.base % 512))
rec(event="base", mib=a.base, reserved=torch.cuda.memory_reserved() // MiB)
end = time.time() + a.seconds
if a.role == "neighbour":
    x = torch.randn(2048, 2048, device="cuda"); last = time.time(); ooms = 0
    while time.time() < end:
        y = x @ x
        if time.time() - last >= 0.1:
            last = time.time()
            try:
                t = take(1024); del t
            except torch.OutOfMemoryError as e:
                ooms += 1; rec(event="oom", msg=str(e).splitlines()[0][:200])
            torch.cuda.empty_cache()
            rec(event="beat")
    rec(event="end", ooms=ooms)
else:
    time.sleep(10)
    rec(event="over_start", over=a.over, pattern=a.pattern)
    got = 0
    try:
        if a.pattern == "burst":
            held.append(take(a.over)); got = a.over
        else:
            while got < a.over:
                held.append(take(256)); got += 256; rec(event="step", got=got); time.sleep(0.05)
        rec(event="over_done", got=got)
    except torch.OutOfMemoryError as e:
        rec(event="refused", got=got, msg=str(e).splitlines()[0][:200])
    while time.time() < end:
        rec(event="beat"); time.sleep(0.05)
    rec(event="end", got=got)
'''


def free_mib(gpu):
    out = subprocess.run(["nvidia-smi", "-i", str(gpu), "--query-gpu=memory.total,memory.used",
                          "--format=csv,noheader,nounits"], capture_output=True, text=True).stdout
    total, used = [int(x) for x in out.split(",")]
    return total - used


def set_mode(mode, gpu):
    env = dict(os.environ, JOB_GPU_POOL=str(gpu), **MODES[mode])
    restart_backend(env)


def restart_backend(env=None):
    subprocess.run(COMPOSE + ["up", "-d", "backend"], cwd=ROOT, env=env, check=True,
                   capture_output=True)
    for _ in range(60):
        try:
            api._request("GET", "/api/health", timeout=5)
            return
        except Exception:  # noqa: BLE001 (backend still starting)
            time.sleep(2)
    raise SystemExit("dev backend did not come back")


def only_the_master(admin):
    """E1 pins its jobs to one card of the master (JOB_GPU_POOL); a joined node
    would be offered them too, on cards the runner never checked were idle."""
    _, nodes = api._request("GET", "/api/admin/nodes", admin)
    others = [n["name"] for n in nodes if not n["is_local"] and n["ready"]]
    if others:
        raise SystemExit(f"other nodes are ready ({others}); stop them first "
                         "(scripts/experiments/emu_nodes.sh down)")


def setup(admin):
    tokens = {}
    for user in sorted(set(USERS.values())):
        api.ensure_user(admin, user, PASSWORD, gpu_indices=[], memory_limit_mb=16384,
                        cpu_cores=4, max_processes=1024)
        tok = api.login(user, PASSWORD)
        api.start_workspace(tok)
        name = f"{PREFIX}jupyter-{user}"
        api.wait_running(name)
        res = api.sh(name, user, "python3 -c 'import torch' 2>/dev/null && echo have-torch || "
                     "python3 -m pip install --user -q torch==2.4.1 "
                     "--extra-index-url https://download.pytorch.org/whl/cu121 && echo have-torch",
                     timeout=3600)
        if "have-torch" not in res.stdout:
            raise SystemExit(f"torch install for {user} failed: {res.stderr[-1500:]}")
        api.sh(name, user, "mkdir -p ~/e1 && cat > ~/e1/w.py <<'PY'\n" + WORKLOAD + "\nPY")
        for role, u in USERS.items():
            if u == user:
                tokens[role] = tok
    return tokens


def submit(role, args, mib, trial):
    user = USERS[role]
    log = f"e1/{trial}-{role}.jsonl"
    script = f"e1/{trial}-{role}.sh"
    api.sh(f"{PREFIX}jupyter-{user}", user,
           f"cat > ~/{script} <<'SH'\npython3 ~/e1/w.py --role {role} {args} --log ~/{log}\nSH")
    res = api.sh(f"{PREFIX}jupyter-{user}", user,
                 f"cd ~ && submit {script} --gpu-memory {mib} --name e1-{role}")
    jid = int(res.stdout.split("job ")[1].split()[0])
    return jid, HOME_DATA / user / log


def events(path):
    try:
        return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    except FileNotFoundError:
        return []


def trial(tokens, gpu, mode, over, pattern, rep):
    name = f"{mode}-{pattern}-{over}-r{rep}-{int(time.time())}"
    free = free_mib(gpu)
    base = int(free * 0.4) // 256 * 256
    nb_id, nb_log = submit("neighbour", f"--base {base} --seconds 75", base, name)
    for _ in range(120):                       # wait until it holds its memory
        if any(e["event"] == "base" for e in events(nb_log)):
            break
        time.sleep(1)
    of_id, of_log = submit("offender", f"--base {base} --over {over} --pattern {pattern} --seconds 45",
                           base, name)
    _, nb = api._request("GET", f"/api/jobs/{nb_id}", tokens["neighbour"])
    while nb["status"] not in ("succeeded", "failed", "cancelled", "timeout"):
        time.sleep(2)
        _, nb = api._request("GET", f"/api/jobs/{nb_id}", tokens["neighbour"])
    _, of = api._request("GET", f"/api/jobs/{of_id}", tokens["offender"])
    while of["status"] not in ("succeeded", "failed", "cancelled", "timeout"):
        time.sleep(2)
        _, of = api._request("GET", f"/api/jobs/{of_id}", tokens["offender"])
    ne, oe = events(nb_log), events(of_log)
    start = next((e["t"] for e in oe if e["event"] == "over_start"), None)
    refused = next((e for e in oe if e["event"] == "refused"), None)
    last_beat = max((e["t"] for e in oe if e["event"] in ("beat", "step", "over_done")), default=None)
    survived = any(e["event"] == "end" for e in oe)
    ooms = [e for e in ne if e["event"] == "oom"]
    return {
        "trial": name, "mode": mode, "pattern": pattern, "over_mib": over, "rep": rep, "gpu": gpu,
        "free_mib": free, "base_mib": base,
        "neighbour": {"status": nb["status"], "ooms": len(ooms),
                      "first_oom_after_s": round(ooms[0]["t"] - start, 3) if ooms and start else None},
        "offender": {"status": of["status"], "message": of.get("message"),
                     "refused_after_s": round(refused["t"] - start, 3) if refused and start else None,
                     "refused_at_mib": refused["got"] if refused else None,
                     "stopped_after_s": (round(last_beat - start, 3)
                                         if (not survived and last_beat and start) else None),
                     "survived": survived},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--modes", default="monitor,hami,both")
    ap.add_argument("--over", default="2048,8192,16384")
    ap.add_argument("--patterns", default="burst,ramp")
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--setup-only", action="store_true",
                    help="create the account and install PyTorch (no GPU needed)")
    args = ap.parse_args()
    gpu = int((os.environ.get("EXP_GPUS") or "1").split(",")[0])
    out = Path(os.environ.get("EXP_OUT") or ".")

    admin = api.admin_token()
    only_the_master(admin)
    tokens = setup(admin)
    if args.setup_only:
        return
    # The runner starts each attempt in a new directory beside the earlier ones;
    # trials an aborted attempt already recorded are not run again.
    done = set()
    for f in out.parent.glob("*/e1_trials.jsonl"):
        for line in f.read_text().splitlines():
            r = json.loads(line)
            done.add((r["mode"], r["pattern"], r["over_mib"], r["rep"]))
    results = []
    for mode in args.modes.split(","):
        set_mode(mode, gpu)
        tokens = {r: api.login(u, PASSWORD) for r, u in USERS.items()}
        for rep in range(args.reps):
            for over in [int(x) for x in args.over.split(",")]:
                for pattern in args.patterns.split(","):
                    if (mode, pattern, over, rep) in done:
                        continue
                    r = trial(tokens, gpu, mode, over, pattern, rep)
                    results.append(r)
                    print(json.dumps(r), flush=True)
                    with open(out / "e1_trials.jsonl", "a") as fh:
                        fh.write(json.dumps(r) + "\n")
    # Back to the stack's own settings: no pinned card, the shipped enforcement.
    restart_backend()
    for user in set(USERS.values()):
        api.stop_workspace(api.login(user, PASSWORD))
    (out / "e1_summary.json").write_text(json.dumps(
        {"measured": datetime.now().astimezone().isoformat(timespec="seconds"),
         "gpu": gpu, "trials": len(results)}, indent=1))


if __name__ == "__main__":
    main()
