#!/usr/bin/env python3
"""Summarise E4, one block per queue entry (the latest run of each).

    python3 scripts/experiments/e4_summary.py      # writes results/e4.json

Kernel round trips fall in two groups, a few milliseconds and about 40 ms
more (see e4_nodes.py), so where the samples were kept they are reported as
the share in the upper group and the median of each group.
"""

import json
import statistics as st
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"
# The upper group sits about 40 ms above the lower; the split moves with the
# delay added on the way to a remote node.
SPLIT_MS = 25.0


def latest(entry, name):
    runs = sorted(RESULTS.glob(f"runs/{entry}/*/{name}"))
    return runs[-1] if runs else None


def placement(path):
    d = json.loads(path.read_text())
    out = {"path": str(path.relative_to(RESULTS)), "free_mib": d["free_mib"],
           "request_mib": d["request_mib"]}
    for part in ("both_nodes", "emu1_drained"):
        jobs = d.get(part, [])
        nodes = {}
        for j in jobs:
            nodes[j["node"]] = nodes.get(j["node"], 0) + 1
        out[part] = {"jobs": len(jobs), "per_node": nodes,
                     "succeeded": sum(j["status"] == "succeeded" for j in jobs),
                     "right_gpu": sum(bool(j["right_gpu"]) for j in jobs),
                     "wait_s": [j["waited_s"] for j in jobs]}
    return out


def groups(xs, added=0.0):
    low = [x for x in xs if x < SPLIT_MS + added]
    high = [x for x in xs if x >= SPLIT_MS + added]
    return {"n": len(xs), "upper_share": round(len(high) / len(xs), 2),
            "low_median": round(st.median(low), 1) if low else None,
            "high_median": round(st.median(high), 1) if high else None}


def latency(path):
    d = json.loads(path.read_text())
    rows = []
    for r in d["rows"]:
        row = {"delay_ms": r["delay_ms"],
               "http_median_ms": {k: round(v["median"], 1) for k, v in r["http_ms"].items()},
               "http_p95_ms": {k: round(v["p95"], 1) for k, v in r["http_ms"].items()},
               "start_median_s": {k: round(st.median(v), 2) for k, v in r["start_s"].items()}}
        if "ws_rtt_raw" in r:
            row["ws"] = {k: groups(v, r["delay_ms"] if k == "remote" else 0.0)
                         for k, v in r["ws_rtt_raw"].items()}
        else:
            row["ws_median_ms"] = {k: round(v["median"], 1) for k, v in r["ws_rtt_ms"].items()}
        rows.append(row)
    return {"path": str(path.relative_to(RESULTS)), "rows": rows}


def failure(path):
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    out = {"path": str(path.relative_to(RESULTS)), "by_outage": []}
    for down in sorted({r["down_s"] for r in rows}):
        rs = [r for r in rows if r["down_s"] == down]
        nr = [r["node_not_ready_after_s"] for r in rs if r["node_not_ready_after_s"] is not None]
        out["by_outage"].append({
            "down_s": down, "reps": len(rs),
            "succeeded": sum(r["job_status"] == "succeeded" for r in rs),
            "ticks": sorted({r["ticks"] for r in rs}),
            "not_ready_after_s": [min(nr), max(nr)] if nr else None,
            "ready_after_restart_s": [min(r["node_ready_after_restart_s"] for r in rs),
                                      max(r["node_ready_after_restart_s"] for r in rs)],
            "proxy_codes": sorted({c for r in rs for c in r["proxy_codes_while_down"]}),
            "proxy_back_after_restart_s": max(r["proxy_back_after_restart_s"] for r in rs),
            "ran_s": round(st.median(r["ran_s"] for r in rs), 1),
            "recorded_runtime_s": sorted(r["recorded_runtime_s"] for r in rs),
        })
    return out


def main():
    out = {}
    for entry in sorted({p.name for p in RESULTS.glob("runs/e4-*")}):
        for name, fn in (("e4_placement.json", placement), ("e4_latency.json", latency),
                         ("e4_failure.jsonl", failure)):
            path = latest(entry, name)
            if path:
                out[entry] = fn(path)
    (RESULTS / "e4.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
