#!/usr/bin/env python3
"""How much of the GPU time assigned to interactive workspaces is actually used.

The ledger records how long a workspace with GPUs assigned was open, not what
it did with them.  This sampler fills the gap from the host, once a minute:

* every platform container (``gpu-jupyter-<user>`` workspaces and
  ``gpu-job-<id>`` batch jobs) and the GPU UUIDs its device request grants;
* per GPU process, the memory it holds and its SM utilisation
  (``nvidia-smi --query-compute-apps`` and ``nvidia-smi pmon``), attributed to
  a container through ``/proc/<pid>/cgroup``; processes outside the platform
  are counted as "other".

    python3 scripts/experiments/workspace_gpu_sampler.py sample --out ~/gpu_samples.jsonl
    python3 scripts/experiments/workspace_gpu_sampler.py summary ~/gpu_samples.jsonl \\
        > paper/data/workspace_gpu_use.json

The raw log names users; ``summary`` replaces them with user1, user2, ... (in
order of appearance in the log, not the numbering of the deployment trace) and
reports, for workspaces with a GPU assigned, the share of samples in which the
assigned GPUs held memory for the workspace and in which its SM utilisation
was above a threshold.  It needs read access to the Docker socket and runs on
the host, not in the platform.
"""

import argparse
import json
import re
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime

_SCOPE = re.compile(r"docker-([0-9a-f]{64})\.scope")


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout


def platform_containers():
    """``{container id: (name, [granted GPU UUIDs])}`` for running platform containers."""
    ids = _run(["docker", "ps", "-q", "--no-trunc", "--filter", "name=^gpu-"]).split()
    if not ids:
        return {}
    out = {}
    for c in json.loads(_run(["docker", "inspect", *ids])):
        name = c["Name"].lstrip("/")
        if not (name.startswith("gpu-jupyter-") or name.startswith("gpu-job-")):
            continue
        uuids = []
        for req in (c.get("HostConfig") or {}).get("DeviceRequests") or []:
            uuids += [d for d in (req.get("DeviceIDs") or []) if d.startswith("GPU-")]
        out[c["Id"]] = (name, uuids)
    return out


def container_of(pid):
    try:
        with open(f"/proc/{pid}/cgroup") as fh:
            m = _SCOPE.search(fh.read())
        return m.group(1) if m else None
    except OSError:
        return None


def sm_by_pid():
    """``{pid: SM %}`` from one ``nvidia-smi pmon`` sample."""
    out = {}
    for line in _run(["nvidia-smi", "pmon", "-c", "1", "-s", "u"]).splitlines():
        parts = line.split()
        if not parts or parts[0].startswith("#") or len(parts) < 4:
            continue
        try:
            out[int(parts[1])] = int(parts[3]) if parts[3].isdigit() else 0
        except ValueError:
            continue
    return out


def sample():
    cards = {}
    for line in _run(["nvidia-smi", "--query-gpu=uuid,index,memory.used,utilization.gpu",
                      "--format=csv,noheader,nounits"]).splitlines():
        uuid, index, used, util = [p.strip() for p in line.split(",")]
        cards[uuid] = {"index": int(index), "used_mib": int(used), "util": int(util)}
    containers = platform_containers()
    sm = sm_by_pid()
    procs = []
    for line in _run(["nvidia-smi", "--query-compute-apps=gpu_uuid,pid,used_gpu_memory",
                      "--format=csv,noheader,nounits"]).splitlines():
        uuid, pid, mib = [p.strip() for p in line.split(",")]
        cid = container_of(int(pid))
        owner = containers[cid][0] if cid in containers else "other"
        procs.append({"gpu": uuid, "owner": owner,
                      "mib": int(mib) if mib.isdigit() else 0, "sm": sm.get(int(pid), 0)})
    return {"t": datetime.now().astimezone().isoformat(timespec="seconds"),
            "cards": cards, "containers": {n: g for n, g in containers.values()},
            "procs": procs}


def run_sample(args):
    with open(args.out, "a") as fh:
        while True:
            try:
                fh.write(json.dumps(sample()) + "\n")
                fh.flush()
            except Exception as exc:  # noqa: BLE001  (keep sampling through a hiccup)
                print(f"{datetime.now():%F %T} sample failed: {exc}", file=sys.stderr)
            if args.once:
                return
            time.sleep(args.interval)


def run_summary(args):
    rows = [json.loads(line) for line in open(args.log) if line.strip()]
    alias = {}

    def who(name):
        user = name[len("gpu-jupyter-"):]
        return alias.setdefault(user, f"user{len(alias) + 1}")

    per_ws = defaultdict(lambda: {"samples": 0, "gpu_samples": 0, "holding": 0, "active": 0,
                                  "held_mib": []})
    other_mib = []
    for r in rows:
        held = defaultdict(int)
        busy = defaultdict(int)
        for p in r["procs"]:
            if p["owner"] == "other":
                continue
            held[(p["owner"], p["gpu"])] += p["mib"]
            busy[(p["owner"], p["gpu"])] = max(busy[(p["owner"], p["gpu"])], p["sm"])
        other_mib.append(sum(p["mib"] for p in r["procs"] if p["owner"] == "other"))
        for name, uuids in r["containers"].items():
            if not name.startswith("gpu-jupyter-"):
                continue
            s = per_ws[who(name)]
            s["samples"] += 1
            for uuid in uuids:              # one GPU-sample per assigned card
                s["gpu_samples"] += 1
                mib = held.get((name, uuid), 0)
                s["held_mib"].append(mib)
                s["holding"] += mib > 0
                s["active"] += busy.get((name, uuid), 0) >= args.active_sm
    out = {"computed": datetime.now().astimezone().isoformat(timespec="seconds"),
           "first": rows[0]["t"] if rows else None, "last": rows[-1]["t"] if rows else None,
           "samples": len(rows), "active_sm_threshold": args.active_sm, "workspaces": {}}
    tot = {"gpu_samples": 0, "holding": 0, "active": 0}
    for user, s in sorted(per_ws.items()):
        if not s["gpu_samples"]:
            continue
        for k in tot:
            tot[k] += s[k]
        out["workspaces"][user] = {
            "gpu_hours_assigned": round(s["gpu_samples"] * args.interval / 3600, 1),
            "share_holding_memory": round(s["holding"] / s["gpu_samples"], 3),
            "share_active": round(s["active"] / s["gpu_samples"], 3),
            "median_held_mib": statistics.median(s["held_mib"])}
    if tot["gpu_samples"]:
        out["all_workspaces"] = {
            "gpu_hours_assigned": round(tot["gpu_samples"] * args.interval / 3600, 1),
            "share_holding_memory": round(tot["holding"] / tot["gpu_samples"], 3),
            "share_active": round(tot["active"] / tot["gpu_samples"], 3)}
    out["outside_platform_mib"] = {"median": statistics.median(other_mib) if other_mib else None,
                                   "max": max(other_mib, default=None)}
    print(json.dumps(out, indent=1))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample")
    s.add_argument("--out", required=True)
    s.add_argument("--interval", type=float, default=60.0)
    s.add_argument("--once", action="store_true")
    m = sub.add_parser("summary")
    m.add_argument("log")
    m.add_argument("--interval", type=float, default=60.0, help="as sampled")
    m.add_argument("--active-sm", type=int, default=5, help="SM %% that counts as in use")
    args = ap.parse_args()
    {"sample": run_sample, "summary": run_summary}[args.cmd](args)


if __name__ == "__main__":
    main()
