#!/usr/bin/env python3
"""Operational statistics of a deployment, from its database (read-only).

Everything the paper reports about the reference deployment comes from here,
with the date it was computed, so a figure can be traced to a query rather than
to a moment someone looked at a dashboard.

    python3 scripts/experiments/deployment_stats.py data/gpu_platform.db \\
        --until 2026-10-21 --exclude-users admin,demouser,expa,expb,expc,expw \\
        --anonymise > paper/data/deployment.json
"""

import argparse
import json
import sqlite3
import statistics
from datetime import datetime


def ts(v):
    return datetime.fromisoformat(str(v)) if v else None


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * (len(xs) - 1)))] if xs else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--until", help="ignore anything created after this date (YYYY-MM-DD)")
    ap.add_argument("--exclude-users", default="", help="comma-separated, e.g. demo accounts")
    ap.add_argument("--anonymise", action="store_true",
                    help="name users user1, user2, ... in order of first activity, "
                         "as fairshare_sim.py export-trace does")
    args = ap.parse_args()
    excluded = {u for u in args.exclude_users.split(",") if u}
    until = args.until + " 23:59:59.999999" if args.until else "9999"

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    q = lambda s, *p: con.execute(s, p).fetchall()  # noqa: E731

    jobs = [r for r in q("select id, username, status, gpu_count, gpu_memory_mb, "
                         "gpu_memory_peak_mb, created_at, started_at, finished_at, "
                         "runtime_seconds, message, cpu_cores from jobs where created_at <= ?", until)
            if r[1] not in excluded]
    usage = [r for r in q("select username, backend, started_at, ended_at, gpu_seconds, "
                          "cpu_seconds, end_reason, gpu_count, cpu_cores from usage_records "
                          "where started_at <= ?", until) if r[0] not in excluded]
    # A row still open has booked nothing yet.  Charge it up to the cutoff at
    # the rates it holds, as the fair-share score does, or a workspace that has
    # been open for weeks counts for nothing at all.
    cutoff = ts(until) if args.until else datetime.now()
    usage = [u if u[3] else
             u[:3] + (None,
                      (cutoff - ts(u[2])).total_seconds() * (u[7] or 0),
                      (cutoff - ts(u[2])).total_seconds() * (u[8] or 0.0)) + u[6:]
             for u in usage]
    users_active = sorted({r[1] for r in jobs} | {r[0] for r in usage})
    alias = {u: u for u in users_active}
    if args.anonymise:
        # The trace counts started jobs and container sessions, so its order is
        # taken from the same events.
        events = sorted([(ts(j[6]), j[1]) for j in jobs if j[7]] +
                        [(ts(u[2]), u[0]) for u in usage if u[1] == "container"])
        alias = {}
        for _, user in events:
            alias.setdefault(user, f"user{len(alias) + 1}")
        for user in users_active:
            alias.setdefault(user, f"user{len(alias) + 1}")
    accounts = [r[0] for r in q("select username from users") if r[0] not in excluded]

    started = [j for j in jobs if j[7]]
    waits = [(ts(j[7]) - ts(j[6])).total_seconds() for j in started]
    gpu_waits = [(ts(j[7]) - ts(j[6])).total_seconds() for j in started if (j[3] or 0) > 0]
    statuses = {}
    for j in jobs:
        statuses[j[2]] = statuses.get(j[2], 0) + 1
    overrun = [j for j in jobs if j[10] and j[10].startswith("Stopped: it was holding")]
    oom = [j for j in jobs if j[10] and ("ran out of memory" in j[10] or "OOM" in j[10])]

    interactive = [u for u in usage if u[1] != "job"]
    batch = [u for u in usage if u[1] == "job"]
    first = min(ts(u[2]) for u in usage)
    last = max(ts(u[3]) if u[3] else cutoff for u in usage)

    out = {
        "computed": datetime.now().astimezone().isoformat(timespec="seconds"),
        "until": args.until,
        "excluded_users": sorted(excluded) if not args.anonymise else len(excluded),
        "period": {"first": first.isoformat(timespec="minutes"),
                   "last": last.isoformat(timespec="minutes"),
                   "days": round((last - first).total_seconds() / 86400, 1)},
        "accounts": len(accounts),
        "users_with_activity": len(users_active),
        "jobs": {
            "submitted": len(jobs),
            "started": len(started),
            "gpu": sum(1 for j in jobs if (j[3] or 0) > 0),
            "cpu_only": sum(1 for j in jobs if not (j[3] or 0)),
            "multi_gpu": sum(1 for j in jobs if (j[3] or 0) > 1),
            "by_status": statuses,
            "per_user": {alias[u]: sum(1 for j in jobs if j[1] == u)
                         for u in sorted(users_active, key=lambda u: alias[u])},
            "stopped_for_vram_overrun": len(overrun),
            "oom_killed": len(oom),
            "wait_s": {"median": statistics.median(waits) if waits else None,
                       "p90": pct(waits, 0.9), "p99": pct(waits, 0.99), "max": max(waits, default=None)},
            "gpu_wait_s": {"median": statistics.median(gpu_waits) if gpu_waits else None,
                           "p90": pct(gpu_waits, 0.9), "max": max(gpu_waits, default=None)},
        },
        # Interactive hours are hours a workspace with that many GPUs assigned
        # was open, not hours its GPUs were busy.
        "gpu_hours": {
            "batch": round(sum(u[4] or 0 for u in batch) / 3600, 1),
            "interactive": round(sum(u[4] or 0 for u in interactive) / 3600, 1),
        },
        "cpu_core_hours": {
            "batch": round(sum(u[5] or 0 for u in batch) / 3600, 1),
            "interactive": round(sum(u[5] or 0 for u in interactive) / 3600, 1),
        },
        "interactive_sessions": {
            "count": len(interactive),
            "open_at_cutoff": sum(1 for u in interactive if u[3] is None),
            "by_end_reason": {r: sum(1 for u in interactive if u[6] == r)
                              for r in sorted({u[6] for u in interactive}, key=str)},
        },
    }
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
