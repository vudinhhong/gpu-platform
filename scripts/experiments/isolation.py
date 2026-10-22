#!/usr/bin/env python3
"""Does one tenant exhausting a resource affect a co-located tenant?

Two experiment accounts get the same small allocation (no GPU).  Tenant B runs
a fixed set of probes; tenant A runs nothing (baseline) or one of four
"noisy neighbour" workloads at the same time:

    cpu   more busy processes than A has cores
    mem   allocates until the kernel kills it at A's memory.max
    fork  forks until A's pids.max refuses, then holds every process
    io    writes to disk with direct I/O as fast as it is allowed

B's probes:

    cpu_ops     single-thread Python loop iterations per second
    mem_ms      time to allocate and touch 512 MiB
    spawn_ms    time to start 200 short processes
    disk_mbs    direct-I/O write throughput of 200 MiB

Each condition is repeated --reps times, alternating conditions so a slow
drift of the host's own load does not land on one condition.  Every attack
runs inside A's own container, so it is held to A's limits: nothing here can
exhaust the host.  Output is JSON (raw trials) plus a summary table.

    ADMIN_PASSWORD=... python3 scripts/experiments/isolation.py --reps 5 \
        --out paper/data/isolation.json
"""

import argparse
import datetime
import json
import os
import statistics
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import platform_api as api  # noqa: E402

A, B = "expa", "expb"
PASSWORD = "Experiment-pass-1"
LIMITS = {"gpu_indices": [], "memory_limit_mb": 4096, "cpu_cores": 4, "max_processes": 256}
ATTACK_SECONDS = 40

PROBES = r'''
set -e
cd ~
python3 - <<'PY'
import json, os, subprocess, time
r = {}
t0 = time.perf_counter(); n = 0
while time.perf_counter() - t0 < 5.0:
    for _ in range(10000): n += 1
r["cpu_ops"] = n / (time.perf_counter() - t0)
t0 = time.perf_counter()
buf = bytearray(512 * 1024 * 1024)
for i in range(0, len(buf), 4096): buf[i] = 1
r["mem_ms"] = (time.perf_counter() - t0) * 1000
del buf
t0 = time.perf_counter()
for _ in range(200): subprocess.run(["/bin/true"])
r["spawn_ms"] = (time.perf_counter() - t0) * 1000
t0 = time.perf_counter()
out = subprocess.run(["dd", "if=/dev/zero", "of=probe.bin", "bs=1M", "count=200",
                      "oflag=direct"], capture_output=True)
dt = time.perf_counter() - t0
os.remove("probe.bin")
r["disk_mbs"] = 200 * 1048576 / dt / 1e6 if out.returncode == 0 else None
print(json.dumps(r))
PY
'''

ATTACKS = {
    "none": None,
    "cpu": f"""timeout {ATTACK_SECONDS} python3 -c '
import multiprocessing as m
def spin():
    while True: pass
ps=[m.Process(target=spin) for _ in range(16)]
[p.start() for p in ps]; [p.join() for p in ps]'""",
    "mem": f"""timeout {ATTACK_SECONDS} python3 -c '
import time
chunks=[]
while True:
    chunks.append(bytearray(64*1024*1024))
    for i in range(0, len(chunks[-1]), 4096): chunks[-1][i]=1
' ; sleep {ATTACK_SECONDS}""",
    "fork": f"""timeout {ATTACK_SECONDS + 5} python3 -c '
import os, time
kids=0
deadline=time.time()+{ATTACK_SECONDS}
try:
    while True:
        if os.fork()==0:
            time.sleep(max(0, deadline-time.time())); os._exit(0)
        kids+=1
except OSError:
    pass
time.sleep(max(0, deadline-time.time()))'""",
    "io": f"""cd ~ && timeout {ATTACK_SECONDS} dd if=/dev/zero of=flood.bin bs=1M count=100000 oflag=direct; rm -f flood.bin""",
}


def counters(name):
    ev = dict(line.split() for line in api.read_cgroup(name, "memory.events").splitlines())
    pids = dict(line.split() for line in api.read_cgroup(name, "pids.events").splitlines())
    cpu = dict(line.split() for line in api.read_cgroup(name, "cpu.stat").splitlines())
    wbytes = 0
    for line in api.read_cgroup(name, "io.stat").splitlines():
        for field in line.split()[1:]:
            k, _, v = field.partition("=")
            if k == "wbytes":
                wbytes += int(v)
    return {"oom_kill": int(ev.get("oom_kill", 0)), "pids_max_hits": int(pids.get("max", 0)),
            "cpu_usec": int(cpu["usage_usec"]), "wbytes": wbytes, "t": time.time()}


def host_mem_available_mib():
    with open("/proc/meminfo") as fh:
        for line in fh:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    return None


def run_trial(attack):
    ca, cb = f"gpu-jupyter-{A}", f"gpu-jupyter-{B}"
    before = counters(ca)
    started = time.time()
    if ATTACKS[attack]:
        api.sh(ca, A, ATTACKS[attack], detach=True)
        time.sleep(8)                       # let the attack reach its ceiling
    host_mem = host_mem_available_mib()
    out = api.sh(cb, B, PROBES, timeout=300)
    if out.returncode != 0:
        raise RuntimeError(f"probe failed under {attack}: {out.stderr[-500:]}")
    probe = json.loads(out.stdout.strip().splitlines()[-1])
    # let the attack run out so trials do not overlap
    if ATTACKS[attack]:
        time.sleep(max(0.0, started + ATTACK_SECONDS + 8 - time.time()))
    after = counters(ca)
    span = after["t"] - before["t"]
    return {"attack": attack, **probe, "host_mem_available_mib": host_mem,
            "a_oom_kills": after["oom_kill"] - before["oom_kill"],
            "a_pids_max_hits": after["pids_max_hits"] - before["pids_max_hits"],
            # what the attacker actually got, averaged over the trial: proof the
            # attack ran at A's ceiling rather than fizzling
            "a_cpu_cores": (after["cpu_usec"] - before["cpu_usec"]) / 1e6 / span,
            "a_write_mbs": (after["wbytes"] - before["wbytes"]) / 1e6 / span}


def summarise(trials):
    rows = []
    base = {k: statistics.mean(t[k] for t in trials if t["attack"] == "none")
            for k in ("cpu_ops", "mem_ms", "spawn_ms", "disk_mbs")}
    for attack in ATTACKS:
        ts = [t for t in trials if t["attack"] == attack]
        row = {"attack": attack, "n": len(ts)}
        for k in ("cpu_ops", "mem_ms", "spawn_ms", "disk_mbs"):
            vals = [t[k] for t in ts]
            row[k] = (statistics.mean(vals), statistics.stdev(vals) if len(vals) > 1 else 0.0,
                      statistics.mean(vals) / base[k])
        row["a_oom_kills"] = sum(t["a_oom_kills"] for t in ts)
        row["a_pids_max_hits"] = sum(t["a_pids_max_hits"] for t in ts)
        row["a_cpu_cores"] = statistics.mean(t["a_cpu_cores"] for t in ts)
        row["a_write_mbs"] = statistics.mean(t["a_write_mbs"] for t in ts)
        rows.append(row)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--out", default="isolation.json")
    ap.add_argument("--keep-accounts", action="store_true")
    args = ap.parse_args()

    admin = api.admin_token()
    tokens = {}
    for user in (A, B):
        api.ensure_user(admin, user, PASSWORD, **LIMITS)
        tokens[user] = api.login(user, PASSWORD)
        api.start_workspace(tokens[user])
        api.wait_running(f"gpu-jupyter-{user}")
    time.sleep(5)

    applied = {u: {f: api.read_cgroup(f"gpu-jupyter-{u}", f).strip()
                   for f in ("memory.max", "cpu.max", "pids.max", "io.max")}
               for u in (A, B)}

    trials = []
    started = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        for rep in range(args.reps):
            for attack in ATTACKS:
                t = run_trial(attack)
                t["rep"] = rep
                trials.append(t)
                print(f"rep {rep} {attack:5s} cpu={t['cpu_ops']:.3g}/s mem={t['mem_ms']:.0f}ms "
                      f"spawn={t['spawn_ms']:.0f}ms disk={t['disk_mbs']:.1f}MB/s "
                      f"A_oom={t['a_oom_kills']} A_pidsmax={t['a_pids_max_hits']} "
                      f"A_cores={t['a_cpu_cores']:.2f} A_write={t['a_write_mbs']:.1f}MB/s", flush=True)
    finally:
        for user in (A, B):
            api.stop_workspace(tokens[user])
        if not args.keep_accounts:
            for user in (A, B):
                api.deactivate(admin, user)

    result = {"started": started, "limits": LIMITS, "applied": applied,
              "attack_seconds": ATTACK_SECONDS, "trials": trials,
              "summary": summarise(trials)}
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as fh:
        json.dump(result, fh, indent=1)

    print(f"\n{'attack':6s} {'cpu ops/s':>18s} {'mem ms':>16s} {'spawn ms':>16s} {'disk MB/s':>16s}  A:oom pidsmax")
    for r in result["summary"]:
        cells = [f"{m:9.3g}±{sd:<6.2g}({rel:4.2f})" for k in ("cpu_ops", "mem_ms", "spawn_ms", "disk_mbs")
                 for (m, sd, rel) in [r[k]]]
        print(f"{r['attack']:6s} " + " ".join(cells) + f"  {r['a_oom_kills']} {r['a_pids_max_hits']}")


if __name__ == "__main__":
    main()
