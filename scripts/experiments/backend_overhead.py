#!/usr/bin/env python3
"""CPU the platform's own containers spend, read from their cgroups.

The backend runs the 1 s VRAM guard, the 10 s scheduler pass, telemetry and
the API in one process, so its cgroup's CPU time over an interval is an upper
bound on what the guard and the scheduler cost together.  Also times one
host-wide nvidia-smi query, which is what each guard tick runs.

    python3 scripts/experiments/backend_overhead.py --seconds 600 > paper/data/backend_overhead.json
"""
import argparse, json, statistics, subprocess, time
from datetime import datetime

def usage_usec(name):
    cid = subprocess.run(["docker", "inspect", "-f", "{{.Id}}", name],
                         capture_output=True, text=True, check=True).stdout.strip()
    with open(f"/sys/fs/cgroup/system.slice/docker-{cid}.scope/cpu.stat") as fh:
        return int(next(l.split()[1] for l in fh if l.startswith("usage_usec")))

def mem_bytes(name):
    cid = subprocess.run(["docker", "inspect", "-f", "{{.Id}}", name],
                         capture_output=True, text=True, check=True).stdout.strip()
    with open(f"/sys/fs/cgroup/system.slice/docker-{cid}.scope/memory.current") as fh:
        return int(fh.read())

ap = argparse.ArgumentParser()
ap.add_argument("--seconds", type=int, default=600)
ap.add_argument("--containers", default="workspace-gpu-backend-1,workspace-gpu-frontend-1")
args = ap.parse_args()
names = args.containers.split(",")

smi = []
for _ in range(20):
    t0 = time.perf_counter()
    subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_gpu_memory",
                    "--format=csv,noheader,nounits"], capture_output=True)
    smi.append((time.perf_counter() - t0) * 1000)

start = {n: usage_usec(n) for n in names}
t0 = time.time()
time.sleep(args.seconds)
elapsed = time.time() - t0
out = {"measured": datetime.now().astimezone().isoformat(timespec="seconds"),
       "seconds": round(elapsed, 1),
       "nvidia_smi_query_ms": {"median": statistics.median(smi), "max": max(smi)},
       "containers": {n: {"cpu_cores_avg": (usage_usec(n) - start[n]) / 1e6 / elapsed,
                          "memory_mib": mem_bytes(n) / 2**20} for n in names}}
print(json.dumps(out, indent=1))
