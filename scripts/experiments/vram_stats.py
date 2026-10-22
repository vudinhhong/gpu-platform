#!/usr/bin/env python3
"""Reserved against held VRAM for every finished GPU job (read-only).

"Held" is the largest value the VRAM guard sampled for the job
(gpu_memory_peak_mb); jobs recorded before the peak column existed carry only
their last sample (gpu_memory_used_mb), and are flagged as such.  Per GPU, in
MiB.

    python3 scripts/experiments/vram_stats.py data/gpu_platform.db --until 2026-10-21 \\
        --exclude-users demouser,admin > paper/data/vram_jobs.json
"""
import argparse, json, sqlite3, statistics
from datetime import datetime

ap = argparse.ArgumentParser()
ap.add_argument("db"); ap.add_argument("--until"); ap.add_argument("--exclude-users", default="")
a = ap.parse_args()
excl = {u for u in a.exclude_users.split(",") if u}
until = a.until + " 23:59:59.999999" if a.until else "9999"
con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
rows = con.execute(
    "select id, username, gpu_memory_mb, gpu_memory_peak_mb, gpu_memory_used_mb, status, message "
    "from jobs where gpu_count > 0 and gpu_memory_mb > 0 and status <> 'running' and created_at <= ? "
    "and coalesce(gpu_memory_peak_mb, gpu_memory_used_mb, 0) > 0", (until,)).fetchall()
allow = lambda r: max(r * 1.1, r + 1024)  # noqa: E731
jobs = [{"id": i, "reserved": r, "held": p or u, "peak_sampled": p is not None,
         "stopped_for_overrun": bool(m and m.startswith("Stopped: it was holding"))}
        for (i, user, r, p, u, s, m) in rows if user not in excl]
ratio = [j["held"] / j["reserved"] for j in jobs]
n = len(jobs)
print(json.dumps({
    "computed": datetime.now().astimezone().isoformat(timespec="seconds"), "until": a.until,
    "definition": "held = largest VRAM the guard sampled for the job (per GPU, MiB); last sample where no peak was recorded",
    "n": n, "with_peak": sum(j["peak_sampled"] for j in jobs),
    "median_held_over_reserved": statistics.median(ratio),
    "share_below_half": sum(x < 0.5 for x in ratio) / n,
    "share_within_10pct_of_request": sum(0.9 <= x <= 1.1 for x in ratio) / n,
    "above_allowance": sum(j["held"] > allow(j["reserved"]) for j in jobs),
    "stopped_for_overrun": sum(j["stopped_for_overrun"] for j in jobs),
    "jobs": jobs}, indent=1))
