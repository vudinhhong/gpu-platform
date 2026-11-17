#!/usr/bin/env python3
"""Summarise E2: one row per number of concurrent accounts, pooled over the
repetitions (the last run of a level and repetition wins), kept apart per
queue entry (e2-users ran before the fixes, e2-users-cached after).

    python3 scripts/experiments/e2_summary.py      # writes results/e2.json
"""

import json
import math
import statistics as st
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"


def pct(xs, q):
    xs = sorted(xs)
    return xs[math.ceil(q * len(xs)) - 1] if xs else None


def main():
    out = {}
    for entry in sorted({p.name for p in RESULTS.glob("runs/e2-*")}):
        out[entry] = summarise(sorted(RESULTS.glob(f"runs/{entry}/*/e2_levels.jsonl")))
    (RESULTS / "e2.json").write_text(json.dumps(out, indent=1))
    for entry, rows in out.items():
        print(entry)
        for r in rows:
            print(" ", json.dumps(r))


def summarise(paths):
    rows = {}
    raws = {}
    for path in paths:
        for line in path.read_text().splitlines():
            r = json.loads(line)
            rows[(r["n"], r["rep"])] = r
            raws[(r["n"], r["rep"])] = path.parent / f"e2_raw_{r['n']}_r{r['rep']}.json"
    out = []
    for n in sorted({k[0] for k in rows}):
        reps = [rows[k] for k in sorted(rows) if k[0] == n]
        starts, api = [], {"submit": [], "list": [], "queue": []}
        for k in sorted(rows):
            if k[0] != n:
                continue
            raw = json.loads(raws[k].read_text())
            starts += [s["ready_s"] for s in raw["starts"] if s["ready_s"]]
            for kind, ms, code in raw["requests"]:
                if code < 400:
                    api[kind].append(ms)
        sched = [r["scheduler_ms"]["median"] for r in reps if r["scheduler_ms"]]
        out.append({
            "users": n, "reps": len(reps),
            "start_ready_s": {"median": round(st.median(starts), 2), "max": round(max(starts), 2)},
            "start_failed": sum(r["start_failed"] for r in reps),
            "api_ms": {k: {"median": round(st.median(v), 1), "p95": round(pct(v, 0.95), 1)}
                       for k, v in api.items() if v},
            "request_errors": sum(r["request_errors"] for r in reps),
            "timeouts": sum(r.get("timeouts", 0) for r in reps),
            "scheduler_ms_median": round(st.median(sched), 1) if sched else None,
            "scheduler_ms_max": max((r["scheduler_ms"]["max"] for r in reps if r["scheduler_ms"]),
                                    default=None),
            "backend_cpu_cores": round(st.mean(r["backend_cpu_cores"] for r in reps), 3),
            "backend_mem_mib": round(max(r["backend_mem_mib"] for r in reps), 1),
            "job_wait_s_median": round(st.median(r["job_wait_s"]["median"] for r in reps
                                                 if r["job_wait_s"]), 1),
        })
    return out


if __name__ == "__main__":
    main()
