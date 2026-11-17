#!/usr/bin/env python3
"""Summarise E3 and set the simulator against it on the same jobs.

Reads every results/runs/e3-*/*/e3_run.json (the last run of a system,
policy and seed wins), and for each seed also simulates the same job list
with fairshare_sim at the experiment's settings.  At 1/60 of the time a
real second is a simulated minute, so waits are reported in simulated
minutes as they come.

    python3 scripts/experiments/e3_summary.py      # writes results/e3.json
"""

import json
import os
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fairshare_sim as fs  # noqa: E402
from e3_workload import workload  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results"


def waits(rows):
    heavy = [r["wait"] for r in rows if r["user"] == 1 and r["wait"] is not None]
    light = [r["wait"] for r in rows if r["user"] != 1 and r["wait"] is not None]
    return {"light_mean_min": round(st.mean(light), 1) if light else None,
            "heavy_mean_min": round(st.mean(heavy), 1) if heavy else None,
            "light_p90_min": round(sorted(light)[int(0.9 * (len(light) - 1))], 1) if light else None,
            "finished": len(heavy) + len(light), "jobs": len(rows)}


def simulate(seed, users, hours, free_mib):
    jobs = [fs.Job(id=j["k"], user=str(j["user"]), submit=j["submit"], duration=j["duration"],
                   gpu_count=1, request_mb=j["request_mb"], actual_mb=0, cores=0.5)
            for j in workload(seed, users, hours)]
    cfg = fs.Config(gpus=1, vram_mb=free_mib, period=1.0, max_running_per_user=2,
                    window_h=0.4, halflife_h=0.1, lookahead_s=10.0)
    out = {}
    for policy in ("fifo", "slurm", "platform"):
        done = fs.simulate(jobs, cfg, policy)
        rows = [{"user": int(j.user), "wait": (j.start - j.submit) if j.start is not None else None}
                for j in done]
        out[policy] = waits(rows)
    return out


def main():
    runs = {}
    # Oldest first, by the time in the run's directory name, so that the
    # latest run of a system, policy and seed wins whatever its entry is called.
    for path in sorted(RESULTS.glob("runs/e3-*/*/e3_run.json"), key=lambda p: p.parent.name):
        if path.parent.parent.name == "e3-platform-smoke":
            continue
        d = json.loads(path.read_text())
        m = d["meta"]
        if any(r["wait"] is None for r in d["jobs"]):
            continue                   # an incomplete run; it is run again
        runs[(m["system"], m["policy"], m["seed"])] = {"meta": m, **waits(d["jobs"]),
                                                       "path": str(path.relative_to(RESULTS))}
    seeds = sorted({k[2] for k in runs})
    sims = {}
    for seed in seeds:
        meta = next(r["meta"] for k, r in runs.items() if k[2] == seed)
        free = next((r["meta"]["free_mib_at_start"] for k, r in runs.items()
                     if k[2] == seed and k[0] == "platform"), 49140)
        sims[seed] = simulate(seed, meta["users"], meta["hours"], free)
    out = {"runs": [{"system": k[0], "policy": k[1], "seed": k[2], **v} for k, v in sorted(runs.items())],
           "simulated": sims}
    (RESULTS / "e3.json").write_text(json.dumps(out, indent=1))
    for r in out["runs"]:
        print(r["system"], r["policy"], r["seed"], r["light_mean_min"], r["heavy_mean_min"],
              f"{r['finished']}/{r['jobs']}")
    for seed, s in sims.items():
        for p, v in s.items():
            print("sim", p, seed, v["light_mean_min"], v["heavy_mean_min"])


if __name__ == "__main__":
    main()
