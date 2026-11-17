#!/usr/bin/env python3
"""Collect the E1 and E1b trials from results/runs/ into results/e1.json.

The runner retries an experiment that was interrupted, and before the scripts
learnt to skip finished trials a retry repeated some of them, so a trial key
can appear more than once; the last run counts.
"""

import json
import statistics as st
from pathlib import Path

RUNS = Path(__file__).parent / "results" / "runs"
OUT = Path(__file__).parent / "results" / "e1.json"


def load(pattern, key):
    rows = {}
    for f in sorted(RUNS.glob(pattern)):
        for line in f.read_text().splitlines():
            r = json.loads(line)
            rows[key(r)] = r
    return list(rows.values())


def spread(xs):
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    return {"n": len(xs), "min": min(xs), "median": st.median(xs), "max": max(xs)}


def e1():
    rows = load("e1-*/*/e1_trials.jsonl", lambda r: (r["mode"], r["pattern"], r["over_mib"], r["rep"]))
    out = {}
    for mode in ("monitor", "hami", "both"):
        rs = [r for r in rows if r["mode"] == mode]
        by = {}
        for pattern in ("burst", "ramp"):
            p = [r for r in rs if r["pattern"] == pattern]
            o = [r["offender"] for r in p]
            by[pattern] = {
                "trials": len(p),
                "neighbour_oom": sum(r["neighbour"]["ooms"] > 0 for r in p),
                "offender_stopped": sum(x["stopped_after_s"] is not None for x in o),
                "offender_refused": sum(x["refused_after_s"] is not None for x in o),
                "offender_survived": sum(x["survived"] for x in o),
                "stopped_after_s": spread([x["stopped_after_s"] for x in o]),
                "refused_after_s": spread([x["refused_after_s"] for x in o]),
                "refused_at_mib": spread([x["refused_at_mib"] for x in o]),
                "over_mib": sorted({r["over_mib"] for r in p}),
            }
        out[mode] = {"trials": len(rs), "gpus": sorted({r.get("gpu", 1) for r in rs}),
                     "free_mib": sorted({r["free_mib"] for r in rs}),
                     "base_mib": sorted({r["base_mib"] for r in rs}), **by}
    return out


def e1b():
    rows = load("e1b-*/*/e1b_trials.jsonl", lambda r: (r["mode"], r["pattern"], r["delay_s"], r["rep"]))
    out = {}
    for mode in ("monitor", "hami"):
        rs = [r for r in rows if r["mode"] == mode]
        cells = []
        for pattern in ("burst", "ramp"):
            for d in sorted({r["delay_s"] for r in rs}):
                c = [r for r in rs if r["pattern"] == pattern and r["delay_s"] == d]
                o = [r["offender"] for r in c]
                cells.append({
                    "pattern": pattern, "delay_s": d, "trials": len(c),
                    "neighbour_oom": sum(r["neighbour"]["grow"] == "grow_oom" for r in c),
                    "offender_stopped": sum(x["stopped_after_s"] is not None for x in o),
                    "offender_refused": sum(x["refused_after_s"] is not None for x in o),
                    "stopped_after_s": spread([x["stopped_after_s"] for x in o]),
                    "refused_at_mib": spread([x["refused_at_mib"] for x in o]),
                })
        r0 = rs[0]
        out[mode] = {"trials": len(rs), "free_mib": r0["free_mib"], "request_mib": r0["request_mib"],
                     "hold_mib": r0["hold_mib"], "over_mib": r0["over_mib"],
                     "neighbour_oom": sum(r["neighbour"]["grow"] == "grow_oom" for r in rs),
                     "cells": cells}
    return out


if __name__ == "__main__":
    res = {"source": "results/runs/e1-*, e1b-*", "e1": e1(), "e1b": e1b()}
    OUT.write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))
