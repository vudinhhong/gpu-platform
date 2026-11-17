"""The E3 job list, shared by the harness and the summary (which also feeds it
to the simulator).  Times are real seconds at 1/SCALE of simulated time."""

import math
import random

SCALE = 60.0                 # simulated seconds per real second


def workload(seed, n_users=8, hours=24.0, burst=20):
    """Jobs as dicts, times in real seconds (already divided by SCALE)."""
    rng = random.Random(seed)
    jobs = []

    def make(user, t):
        dur = min(12 * 3600, rng.lognormvariate(math.log(1800), 0.9)) / SCALE
        req = int(min(20480, max(1024, rng.lognormvariate(math.log(8192), 0.5))))
        jobs.append({"user": user, "submit": round(t / SCALE, 2),
                     "duration": max(2, round(dur)), "request_mb": req})

    for b in range(int(hours // 12)):
        for _ in range(burst):
            make(1, b * 12 * 3600 + rng.uniform(0, 60))
    for u in range(2, n_users + 1):
        t = rng.uniform(0, 3600)
        while t < hours * 3600:
            make(u, t)
            t += rng.expovariate(1 / (2 * 3600))
    jobs.sort(key=lambda j: j["submit"])
    for i, j in enumerate(jobs):
        j["k"] = i
    return jobs
