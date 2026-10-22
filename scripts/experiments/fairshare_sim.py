#!/usr/bin/env python3
"""Discrete-time simulation of the job queue under several ordering policies.

The dispatcher is a re-implementation of ``services/jobs._dispatch_queued`` and
``services/fairshare`` without the database, so a policy can be evaluated on
workloads larger, busier and more varied than one deployment produces:

* a scheduling pass every ``period`` seconds;
* a per-user cap on running jobs;
* GPU placement by free VRAM, densest fit first, where a card's free memory is
  its total minus the larger of (a) what running jobs actually hold and
  (b) the allowances of the jobs admitted to it, minus a fixed headroom;
* CPU admission against a core pool (the pessimistic "allocated" reading);
* a job that does not fit is skipped, never allowed to block the queue.

Only the ordering differs between policies:

``fifo``      submission time.
``slurm``     decayed past usage only, the ranking Slurm's classic fair-share
              factor 2^(-U/S) produces when every user has an equal share
              (Fair Tree gives the same ranking for a flat account tree).
              Running work accrues usage as it runs; nothing is charged at
              admission.
``platform``  the platform's score: decayed usage within a window W, plus
              ``L`` seconds of every running job's resources, plus the same
              charge applied to a job's owner the moment it is admitted
              within the pass.

Workloads: a replay of the deployment's own job table (read-only), optionally
with arrivals compressed to raise contention, or a synthetic mix of users.

    python3 scripts/experiments/fairshare_sim.py synthetic --out paper/data/sim_synthetic.json
    python3 scripts/experiments/fairshare_sim.py replay --db data/gpu_platform.db --out ...
    python3 scripts/experiments/fairshare_sim.py sweep --out ...
    python3 scripts/experiments/fairshare_sim.py vram --db data/gpu_platform.db --out ...
"""

import argparse
import dataclasses
import json
import math
import random
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional

GIB = 1024


@dataclasses.dataclass
class Config:
    gpus: int = 2
    vram_mb: int = 49140
    headroom_mb: int = 512
    cores: float = 28.0            # 32 cores minus JOB_CPU_RESERVED_CORES
    period: float = 10.0
    max_running_per_user: int = 2
    window_h: float = 24.0
    halflife_h: float = 6.0
    kappa: float = 0.125
    lookahead_s: float = 600.0
    overrun_factor: float = 1.1
    overrun_grace_mb: int = 1024


@dataclasses.dataclass
class Job:
    id: int
    user: str
    submit: float
    duration: float
    gpu_count: int
    request_mb: int                # per GPU; 0 for a CPU job
    actual_mb: int                 # per GPU, what it really holds
    cores: float
    start: Optional[float] = None
    end: Optional[float] = None
    cards: tuple = ()


def allowance(cfg: Config, request_mb: int) -> int:
    if not request_mb:
        return 0
    return int(max(request_mb * cfg.overrun_factor, request_mb + cfg.overrun_grace_mb))


def simulate(jobs: List[Job], cfg: Config, policy: str, horizon: Optional[float] = None) -> List[Job]:
    jobs = [dataclasses.replace(j) for j in jobs]
    pending = sorted(jobs, key=lambda j: (j.submit, j.id))
    queued: List[Job] = []
    running: List[Job] = []
    done: List[Job] = []
    # (user, start, end, weighted rate) for finished stretches
    ledger = []
    now = 0.0
    limit = horizon or (max(j.submit for j in jobs) + sum(j.duration for j in jobs) + 1)

    def weight(j):
        return j.gpu_count + cfg.kappa * j.cores

    def history(t):
        since = t - cfg.window_h * 3600 if policy == "platform" else -math.inf
        hl = cfg.halflife_h * 3600
        s = defaultdict(float)
        for user, start, end, rate in ledger:
            inside = max(0.0, end - max(start, since))
            if inside <= 0:
                continue
            s[user] += rate * inside * 0.5 ** ((t - end) / hl)
        for j in running:
            inside = max(0.0, t - max(j.start, since))
            s[j.user] += weight(j) * inside
        return s

    while (pending or queued or running) and now <= limit:
        for j in [j for j in running if j.start + j.duration <= now]:
            j.end = j.start + j.duration
            running.remove(j)
            done.append(j)
            ledger.append((j.user, j.start, j.end, weight(j)))
        while pending and pending[0].submit <= now:
            queued.append(pending.pop(0))

        if queued:
            per_user = defaultdict(int)
            for j in running:
                per_user[j.user] += 1
            held = defaultdict(int)
            entitled = defaultdict(int)
            for j in running:
                for c in j.cards:
                    held[c] += j.actual_mb
                    entitled[c] += allowance(cfg, j.request_mb)
            free = {c: max(0, cfg.vram_mb - max(held[c], entitled[c]) - cfg.headroom_mb)
                    for c in range(cfg.gpus)}
            free_cores = cfg.cores - sum(j.cores for j in running)

            if policy == "fifo":
                score = defaultdict(float)
            else:
                score = history(now)
                if policy == "platform":
                    for j in running:
                        score[j.user] += weight(j) * cfg.lookahead_s

            remaining = list(queued)
            while remaining:
                remaining.sort(key=lambda j: (score.get(j.user, 0.0), j.submit, j.id))
                j = remaining.pop(0)
                if per_user[j.user] >= cfg.max_running_per_user:
                    continue
                if j.cores > free_cores + 1e-9:
                    continue
                cards = ()
                if j.gpu_count:
                    fits = sorted((c for c in range(cfg.gpus) if free[c] >= j.request_mb),
                                  key=lambda c: (free[c], c))
                    if len(fits) < j.gpu_count:
                        continue
                    cards = tuple(fits[: j.gpu_count])
                j.start, j.cards = now, cards
                queued.remove(j)
                running.append(j)
                per_user[j.user] += 1
                free_cores -= j.cores
                for c in cards:
                    free[c] = max(0, free[c] - allowance(cfg, j.request_mb))
                if policy == "platform":
                    score[j.user] += weight(j) * cfg.lookahead_s

        # jump ahead when nothing can change before the next arrival
        nxt = now + cfg.period
        if not queued and not running and pending:
            nxt = max(nxt, math.ceil(pending[0].submit / cfg.period) * cfg.period)
        now = nxt

    for j in running:
        j.end = j.start + j.duration
        done.append(j)
    return done + queued + pending


def metrics(jobs: List[Job], cfg: Config) -> Dict:
    ran = [j for j in jobs if j.start is not None]
    if not ran:
        return {}
    waits = [j.start - j.submit for j in ran]
    bsld = [max(1.0, (j.start - j.submit + j.duration) / max(j.duration, 60.0)) for j in ran]
    t0 = min(j.submit for j in ran)
    t1 = max(j.end for j in ran)
    span = max(1.0, t1 - t0)
    # Card occupancy: the fraction of time a card has at least one job on it.
    # Several jobs can share a card (placement is by free VRAM), so summing
    # job-time would exceed 1; the union of busy intervals does not.
    busy = 0.0
    for card in range(cfg.gpus):
        spans = sorted((j.start, j.end) for j in ran if card in j.cards)
        cur_s = cur_e = None
        for a, b in spans:
            if cur_e is None or a > cur_e:
                if cur_e is not None:
                    busy += cur_e - cur_s
                cur_s, cur_e = a, b
            else:
                cur_e = max(cur_e, b)
        if cur_e is not None:
            busy += cur_e - cur_s
    gpu_job_time = sum(j.gpu_count * j.duration for j in ran)
    vram_used = sum(j.gpu_count * j.actual_mb * j.duration for j in ran)
    vram_reserved = sum(j.gpu_count * allowance(cfg, j.request_mb) * j.duration for j in ran)
    per_user = defaultdict(list)
    for j, w, b in zip(ran, waits, bsld):
        per_user[j.user].append((w, b))
    user_mean_bsld = {u: statistics.mean(b for _, b in v) for u, v in per_user.items()}
    return {
        "jobs": len(ran),
        "unfinished": len(jobs) - len(ran),
        "mean_wait_min": statistics.mean(waits) / 60,
        "p95_wait_min": sorted(waits)[int(0.95 * (len(waits) - 1))] / 60,
        "mean_bsld": statistics.mean(bsld),
        "makespan_h": span / 3600,
        "throughput_jobs_per_h": len(ran) / (span / 3600),
        "gpu_occupancy": busy / (cfg.gpus * span),
        "jobs_per_busy_card": gpu_job_time / busy if busy else None,
        "vram_held_over_reserved": vram_used / vram_reserved if vram_reserved else None,
        "per_user": {u: {"jobs": len(v), "mean_wait_min": statistics.mean(w for w, _ in v) / 60,
                         "mean_bsld": user_mean_bsld[u]} for u, v in per_user.items()},
    }


# ---------------------------------------------------------------------------
# Workloads
# ---------------------------------------------------------------------------

def _ts(value):
    if value is None:
        return None
    return datetime.fromisoformat(str(value)).timestamp()


def load_trace(db_path: str, default_cores: float = 4.0) -> List[Job]:
    """The deployment's job table, read-only.  Jobs that never ran are dropped."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = con.execute(
        "select id, username, created_at, started_at, finished_at, runtime_seconds, "
        "gpu_count, gpu_memory_mb, gpu_memory_peak_mb, gpu_memory_used_mb, cpu_cores "
        "from jobs where started_at is not null").fetchall()
    con.close()
    jobs = []
    for (jid, user, created, started, finished, runtime, gpus, req, peak, used, cores) in rows:
        dur = runtime or ((_ts(finished) - _ts(started)) if finished else None)
        if not dur or dur <= 0:
            continue
        gpus = gpus or 0
        req = req or 0
        actual = peak or used or req
        jobs.append(Job(jid, user, _ts(created), float(dur), gpus, req if gpus else 0,
                        actual if gpus else 0, float(cores or default_cores)))
    t0 = min(j.submit for j in jobs)
    for j in jobs:
        j.submit -= t0
    return sorted(jobs, key=lambda j: j.submit)


def compress(jobs: List[Job], factor: float) -> List[Job]:
    """Divide every arrival time by *factor*: the same jobs, arriving faster."""
    return [dataclasses.replace(j, submit=j.submit / factor) for j in jobs]


def synthetic(n_users: int, seed: int, hours: float = 48.0, heavy_burst: int = 20) -> List[Job]:
    """One heavy user who submits bursts, the rest submitting a job now and then.

    70% GPU jobs (one card, request around 20 GiB, holding 20-100% of it),
    30% CPU jobs (4 cores); durations log-normal with a 30-minute median.
    Light users submit every two hours on average, so the machine is
    oversubscribed during the heavy user's bursts and, with more users, between
    them too.
    """
    rng = random.Random(seed)
    jobs = []
    jid = 0

    def make(user, t):
        nonlocal jid
        jid += 1
        dur = min(12 * 3600, rng.lognormvariate(math.log(1800), 0.9))
        if rng.random() < 0.7:
            req = int(min(46000, max(1024, rng.lognormvariate(math.log(20000), 0.5))))
            act = int(req * rng.uniform(0.2, 1.0))
            return Job(jid, user, t, dur, 1, req, act, 4.0)
        return Job(jid, user, t, dur, 0, 0, 0, 4.0)

    for burst in range(int(hours // 12)):
        for _ in range(heavy_burst):
            jobs.append(make("heavy", burst * 12 * 3600 + rng.uniform(0, 60)))
    for u in range(1, n_users):
        t = rng.uniform(0, 3600)
        while t < hours * 3600:
            jobs.append(make(f"user{u}", t))
            t += rng.expovariate(1 / (2 * 3600))
    return sorted(jobs, key=lambda j: j.submit)


# ---------------------------------------------------------------------------
# Experiments
# ---------------------------------------------------------------------------

POLICIES = ("fifo", "slurm", "platform")


def _agg(runs: List[Dict], keys) -> Dict:
    out = {}
    for k in keys:
        vals = [r[k] for r in runs if r.get(k) is not None]
        if vals:
            out[k] = {"mean": statistics.mean(vals),
                      "sd": statistics.stdev(vals) if len(vals) > 1 else 0.0}
    return out


KEYS = ("mean_wait_min", "p95_wait_min", "mean_bsld", "throughput_jobs_per_h",
        "gpu_occupancy", "jobs_per_busy_card", "makespan_h")


def split_waits(m):
    """Wait of the light users against the heavy one: what fair share trades."""
    light = [v["mean_wait_min"] for u, v in m["per_user"].items() if u != "heavy"]
    m["light_user_wait_min"] = statistics.mean(light) if light else None
    m["worst_light_user_wait_min"] = max(light) if light else None
    m["heavy_user_wait_min"] = m["per_user"].get("heavy", {}).get("mean_wait_min")
    return m


SPLIT = ("light_user_wait_min", "worst_light_user_wait_min", "heavy_user_wait_min")


def run_synthetic(args):
    cfg = Config(max_running_per_user=args.cap)
    out = {}
    for n in (4, 8, 16):
        out[n] = {}
        for policy in POLICIES:
            runs = []
            for seed in range(args.seeds):
                runs.append(split_waits(metrics(simulate(synthetic(n, seed), cfg, policy), cfg)))
            out[n][policy] = _agg(runs, KEYS + SPLIT)
            print(n, policy, {k: round(v["mean"], 2) for k, v in out[n][policy].items()}, flush=True)
    return {"config": dataclasses.asdict(cfg), "seeds": args.seeds, "by_users": out}


def validate(trace, cfg, db_path):
    """Simulated waits against the waits the deployment actually had.

    Only meaningful at the real arrival rate (factor 1): it checks the model
    of the dispatcher, not the policy.
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    real = {r[0]: _ts(r[2]) - _ts(r[1]) for r in con.execute(
        "select id, created_at, started_at from jobs where started_at is not null")}
    con.close()
    sim = {j.id: j.start - j.submit for j in simulate(trace, cfg, "platform") if j.start is not None}
    ids = [i for i in sim if i in real]
    rw = sorted(real[i] for i in ids)
    sw = sorted(sim[i] for i in ids)
    q = lambda xs, p: xs[int(p * (len(xs) - 1))]  # noqa: E731
    return {"jobs": len(ids),
            "real_wait_s": {"median": q(rw, .5), "p90": q(rw, .9), "p99": q(rw, .99)},
            "sim_wait_s": {"median": q(sw, .5), "p90": q(sw, .9), "p99": q(sw, .99)}}


def run_replay(args):
    # The platform admits CPU against *measured* busy cores, which a trace
    # does not record; GPU jobs rarely saturate the host's cores, so CPU is
    # taken as non-binding here and the cards are the constraint.
    cfg = Config(cores=1e9)
    trace = load_trace(args.db)
    out = {"jobs": len(trace), "users": sorted({j.user for j in trace}), "by_factor": {},
           "validation": validate(trace, cfg, args.db)}
    print("validation", out["validation"], flush=True)
    for factor in (1, 4, 16, 64):
        out["by_factor"][factor] = {}
        for policy in POLICIES:
            m = metrics(simulate(compress(trace, factor), cfg, policy), cfg)
            out["by_factor"][factor][policy] = m
            print(factor, policy, {k: (round(m[k], 3) if isinstance(m[k], float) else m[k])
                                   for k in KEYS}, flush=True)
    return {"config": dataclasses.asdict(cfg), **out}


def run_sweep(args):
    base = Config()
    grid = {
        "halflife_h": [1, 3, 6, 12, 24],
        "window_h": [6, 12, 24, 72],
        "kappa": [0.0, 0.0625, 0.125, 0.25, 0.5],
        "lookahead_s": [0, 60, 600, 3600],
        "max_running_per_user": [1, 2, 4, 1000],
    }
    out = {}
    for param, values in grid.items():
        out[param] = {}
        for v in values:
            cfg = dataclasses.replace(base, **{param: v})
            runs = []
            for seed in range(args.seeds):
                runs.append(split_waits(metrics(simulate(synthetic(8, seed), cfg, "platform"), cfg)))
            out[param][v] = _agg(runs, KEYS + SPLIT)
            print(param, v, {k: round(x["mean"], 3) for k, x in out[param][v].items()}, flush=True)
    return {"base": dataclasses.asdict(base), "seeds": args.seeds, "sweep": out}


def run_vram(args):
    """How the size of VRAM requests changes waiting and utilisation (trace)."""
    cfg = Config(cores=1e9)          # as in run_replay
    trace = [j for j in load_trace(args.db)]
    variants = {
        "as_requested": lambda j: j.request_mb,
        "right_sized": lambda j: max(512, int(j.actual_mb)),      # oracle: request = held
        "inflated_x1.5": lambda j: int(j.request_mb * 1.5),
        "deflated_x0.5": lambda j: max(512, int(j.request_mb * 0.5)),
    }
    out = {}
    for factor in (16, 64):
        out[factor] = {}
        for name, fn in variants.items():
            jobs = [dataclasses.replace(j, request_mb=min(cfg.vram_mb - 2048, fn(j)) if j.gpu_count else 0)
                    for j in compress(trace, factor)]
            m = metrics(simulate(jobs, cfg, "platform"), cfg)
            # jobs whose real holding exceeds their allowance would be stopped
            m["would_be_stopped"] = sum(1 for j in jobs if j.gpu_count and j.actual_mb > allowance(cfg, j.request_mb))
            out[factor][name] = m
            print(factor, name, {k: (round(m[k], 3) if isinstance(m[k], float) else m[k])
                                 for k in KEYS + ("vram_held_over_reserved", "would_be_stopped")}, flush=True)
    return {"config": dataclasses.asdict(cfg), "by_factor": out}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("experiment", choices=["synthetic", "replay", "sweep", "vram"])
    ap.add_argument("--db", default="data/gpu_platform.db")
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--cap", type=int, default=2, help="jobs running per user (synthetic)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    result = {"synthetic": run_synthetic, "replay": run_replay,
              "sweep": run_sweep, "vram": run_vram}[args.experiment](args)
    result["generated"] = datetime.now().astimezone().isoformat(timespec="seconds")
    with open(args.out, "w") as fh:
        json.dump(result, fh, indent=1, default=str)


if __name__ == "__main__":
    main()
