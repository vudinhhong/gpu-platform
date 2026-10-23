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

Interactive workspaces can be added.  A workspace holds VRAM on the card its
owner is assigned (``workspace_mb``, which no trace records, so it is a
parameter) and, under ``platform``, is charged to its owner exactly as the
platform charges it: accrued usage while open plus the lookahead.  ``slurm``
and ``fifo`` do not see workspaces, as notebooks run beside a Slurm queue are
not seen by it.

Workloads: a replay of the deployment's jobs and workspace sessions, read from
an anonymised trace (``export-trace`` writes it from the platform database),
optionally with arrivals compressed to raise contention; or a synthetic mix of
users.

    python3 scripts/experiments/fairshare_sim.py export-trace --db data/gpu_platform.db \
        --until 2026-10-21 --out scripts/experiments/results/trace.json
    python3 scripts/experiments/fairshare_sim.py synthetic --out paper/data/sim_synthetic.json
    python3 scripts/experiments/fairshare_sim.py interactive --out ...
    python3 scripts/experiments/fairshare_sim.py replay --trace scripts/experiments/results/trace.json --out ...
    python3 scripts/experiments/fairshare_sim.py sweep --out ...
    python3 scripts/experiments/fairshare_sim.py vram --trace scripts/experiments/results/trace.json --out ...
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
    workspace_mb: int = 0          # VRAM an open workspace holds on each assigned card
    workspace_cores: bool = False  # open workspaces' cores count against the job pool


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
    real_wait: Optional[float] = None   # what the deployment measured (trace only)


@dataclasses.dataclass
class Session:
    """An interactive workspace: open from *start* to *end* on *cards*."""
    user: str
    start: float
    end: float
    cards: tuple
    cores: float


def allowance(cfg: Config, request_mb: int) -> int:
    if not request_mb:
        return 0
    return int(max(request_mb * cfg.overrun_factor, request_mb + cfg.overrun_grace_mb))


def simulate(jobs: List[Job], cfg: Config, policy: str, horizon: Optional[float] = None,
             sessions: List[Session] = ()) -> List[Job]:
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

    def session_weight(w):
        return len(w.cards) + cfg.kappa * w.cores

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
        if policy == "platform":
            # The platform books a workspace like a job: decayed once closed,
            # charged at the rate it holds while open.
            for w in sessions:
                if w.start >= t:
                    continue
                end = min(w.end, t)
                inside = max(0.0, end - max(w.start, since))
                if inside > 0:
                    s[w.user] += session_weight(w) * inside * 0.5 ** ((t - end) / hl)
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
            open_ws = [w for w in sessions if w.start <= now < w.end]
            for w in open_ws:
                for c in w.cards:
                    held[c] += cfg.workspace_mb
            free = {c: max(0, cfg.vram_mb - max(held[c], entitled[c]) - cfg.headroom_mb)
                    for c in range(cfg.gpus)}
            free_cores = cfg.cores - sum(j.cores for j in running)
            if cfg.workspace_cores:
                free_cores -= sum(w.cores for w in open_ws)

            if policy == "fifo":
                score = defaultdict(float)
            else:
                score = history(now)
                if policy == "platform":
                    for j in running:
                        score[j.user] += weight(j) * cfg.lookahead_s
                    for w in open_ws:
                        score[w.user] += session_weight(w) * cfg.lookahead_s

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
        "jain_service": jain_service(jobs, cfg),
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


def jain_service(jobs: List[Job], cfg: Config, horizon: Optional[float] = None) -> Optional[float]:
    """Jain's index over users of the service each received against what they asked for.

    For every user, the resource-seconds (GPUs + kappa x cores) delivered before
    *horizon* divided by the resource-seconds of the jobs they submitted before
    it.  Run to completion every user eventually gets everything, so the index
    is taken while the workload is still arriving: by default at the last
    submission.  1 means every user has been served the same fraction of their
    demand; 1/n means one user has been served and nobody else.
    """
    horizon = horizon if horizon is not None else max(j.submit for j in jobs)
    served, asked = defaultdict(float), defaultdict(float)
    for j in jobs:
        if j.submit > horizon:
            continue
        w = j.gpu_count + cfg.kappa * j.cores
        asked[j.user] += w * j.duration
        if j.start is not None and j.start < horizon:
            served[j.user] += w * (min(j.start + j.duration, horizon) - j.start)
    x = [served[u] / asked[u] for u in asked if asked[u] > 0]
    if not x or not any(x):
        return None
    return sum(x) ** 2 / (len(x) * sum(v * v for v in x))


# ---------------------------------------------------------------------------
# Workloads
# ---------------------------------------------------------------------------

def _ts(value):
    if value is None:
        return None
    return datetime.fromisoformat(str(value)).timestamp()


def export_trace(db_path: str, until: str, exclude: set) -> Dict:
    """Jobs and workspace sessions from the platform database, anonymised.

    Usernames become user1, user2, ... in order of first activity, and times
    become seconds from the first event, so the file carries the shape of the
    workload and nothing that identifies a person or a project.
    """
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    cutoff = until + " 23:59:59.999999"
    jobs = con.execute(
        "select id, username, created_at, started_at, finished_at, runtime_seconds, gpu_count, "
        "gpu_memory_mb, gpu_memory_peak_mb, gpu_memory_used_mb, cpu_cores "
        "from jobs where started_at is not null and created_at <= ?", (cutoff,)).fetchall()
    sessions = con.execute(
        "select username, started_at, ended_at, gpu_indices, cpu_cores, cpu_seconds, gpu_count "
        "from usage_records where backend = 'container' and started_at <= ?", (cutoff,)).fetchall()
    con.close()
    jobs = [r for r in jobs if r[1] not in exclude]
    sessions = [r for r in sessions if r[0] not in exclude]

    events = sorted([(_ts(r[2]), r[1]) for r in jobs] + [(_ts(r[1]), r[0]) for r in sessions])
    t0 = events[0][0]
    alias = {}
    for _, user in events:
        alias.setdefault(user, f"user{len(alias) + 1}")
    end_of_trace = _ts(cutoff)

    out_jobs = []
    for (jid, user, created, started, finished, runtime, gpus, req, peak, used, cores) in jobs:
        dur = runtime or ((_ts(finished) - _ts(started)) if finished else None)
        if not dur or dur <= 0:
            continue
        gpus = gpus or 0
        out_jobs.append({
            "id": len(out_jobs) + 1, "user": alias[user],
            "submit_s": round(_ts(created) - t0, 1), "wait_s": round(_ts(started) - _ts(created), 1),
            "duration_s": round(float(dur), 1), "gpus": gpus,
            "request_mib": (req or 0) if gpus else 0,
            "held_mib": (peak or used or req or 0) if gpus else 0,
            "cores": float(cores or 4.0)})
    out_sessions = []
    for (user, started, ended, indices, cores, cpu_s, gpus) in sessions:
        start, end = _ts(started), _ts(ended) if ended else end_of_trace
        if end <= start:
            continue
        cards = [int(i) for i in (indices or "").split(",") if i.strip().isdigit()]
        # Older rows did not store the core count; the ledger's CPU-seconds do.
        cores = cores or ((cpu_s or 0.0) / (end - start))
        out_sessions.append({"user": alias[user], "start_s": round(start - t0, 1),
                             "end_s": round(end - t0, 1), "cards": cards,
                             "cores": round(float(cores), 2)})
    return {"until": until, "users": len(alias), "jobs": out_jobs, "sessions": out_sessions}


def load_trace(path: str):
    """Jobs and sessions from a trace written by ``export_trace``."""
    with open(path) as fh:
        data = json.load(fh)
    jobs = [Job(j["id"], j["user"], j["submit_s"], j["duration_s"], j["gpus"], j["request_mib"],
                j["held_mib"], j["cores"], real_wait=j["wait_s"]) for j in data["jobs"]]
    sessions = [Session(w["user"], w["start_s"], w["end_s"], tuple(w["cards"]), w["cores"])
                for w in data["sessions"]]
    return sorted(jobs, key=lambda j: j.submit), sessions


def compress(jobs: List[Job], factor: float) -> List[Job]:
    """Divide every arrival time by *factor*: the same jobs, arriving faster."""
    return [dataclasses.replace(j, submit=j.submit / factor) for j in jobs]


def compress_sessions(sessions: List[Session], factor: float) -> List[Session]:
    """Open each workspace *factor* times sooner, for as long as it was open.

    A user has one workspace, so stretches that now overlap are merged.
    """
    out: List[Session] = []
    for w in sorted(sessions, key=lambda w: (w.user, w.start)):
        start = w.start / factor
        end = start + (w.end - w.start)
        last = out[-1] if out and out[-1].user == w.user else None
        if last and start <= last.end:
            last.end = max(last.end, end)
            last.cards = tuple(sorted(set(last.cards) | set(w.cards)))
            last.cores = max(last.cores, w.cores)
        else:
            out.append(Session(w.user, start, end, w.cards, w.cores))
    return out


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
        "gpu_occupancy", "jobs_per_busy_card", "makespan_h", "jain_service")


def split_waits(m, heavy="heavy"):
    """Wait of the light users against the heavy one: what fair share trades."""
    light = [v["mean_wait_min"] for u, v in m["per_user"].items() if u.startswith("user")]
    m["light_user_wait_min"] = statistics.mean(light) if light else None
    m["worst_light_user_wait_min"] = max(light) if light else None
    m["heavy_user_wait_min"] = m["per_user"].get("heavy", {}).get("mean_wait_min")
    m["interactive_user_wait_min"] = m["per_user"].get("inter", {}).get("mean_wait_min")
    return m


SPLIT = ("light_user_wait_min", "worst_light_user_wait_min", "heavy_user_wait_min",
         "interactive_user_wait_min")


def offered_jobs_per_h(jobs: List[Job]) -> float:
    span = max(j.submit for j in jobs) - min(j.submit for j in jobs)
    return len(jobs) / max(1.0, span / 3600)


def run_synthetic(args):
    cfg = Config(max_running_per_user=args.cap)
    out = {}
    for n in (4, 8, 16):
        out[n] = {"offered_jobs_per_h": statistics.mean(
            offered_jobs_per_h(synthetic(n, seed)) for seed in range(args.seeds))}
        for policy in POLICIES:
            runs = []
            for seed in range(args.seeds):
                runs.append(split_waits(metrics(simulate(synthetic(n, seed), cfg, policy), cfg)))
            out[n][policy] = _agg(runs, KEYS + SPLIT)
            print(n, policy, {k: round(v["mean"], 2) for k, v in out[n][policy].items()}, flush=True)
    return {"config": dataclasses.asdict(cfg), "seeds": args.seeds, "by_users": out}


def interactive_mix(n_users: int, seed: int, hours: float = 48.0):
    """The synthetic mix, with one light user replaced by one who also works interactively.

    ``inter`` submits jobs at the light users' rate and, in addition, keeps a
    workspace with one GPU and 8 cores open from 09:00 to 17:00 on each day.
    The question is whether the workspace counts when the queue is ordered.
    """
    jobs = synthetic(n_users, seed, hours)
    for j in jobs:
        if j.user == "user1":
            j.user = "inter"
    sessions = [Session("inter", d * 86400 + 9 * 3600, d * 86400 + 17 * 3600, (0,), 8.0)
                for d in range(int(math.ceil(hours / 24)))]
    return jobs, sessions


def run_interactive(args):
    out = {}
    for ws_mb in WORKSPACE_MB:
        cfg = Config(max_running_per_user=args.cap, workspace_mb=ws_mb)
        out[ws_mb] = {}
        for n in (4, 8, 16):
            out[ws_mb][n] = {}
            for policy in POLICIES:
                runs = []
                for seed in range(args.seeds):
                    jobs, sessions = interactive_mix(n, seed)
                    runs.append(split_waits(metrics(
                        simulate(jobs, cfg, policy, sessions=sessions), cfg)))
                out[ws_mb][n][policy] = _agg(runs, KEYS + SPLIT)
                print(ws_mb, n, policy, {k: round(out[ws_mb][n][policy][k]["mean"], 2)
                                         for k in SPLIT + ("jain_service",)
                                         if k in out[ws_mb][n][policy]}, flush=True)
    return {"config": dataclasses.asdict(Config(max_running_per_user=args.cap)),
            "seeds": args.seeds, "by_workspace_mb": out}


def validate(trace, sessions, cfg):
    """Simulated waits against the waits the deployment actually had.

    Only meaningful at the real arrival rate (factor 1): it checks the model
    of the dispatcher, not the policy.
    """
    sim = {j.id: j.start - j.submit
           for j in simulate(trace, cfg, "platform", sessions=sessions) if j.start is not None}
    real = {j.id: j.real_wait for j in trace if j.real_wait is not None}
    ids = [i for i in sim if i in real]
    rw = sorted(real[i] for i in ids)
    sw = sorted(sim[i] for i in ids)
    q = lambda xs, p: xs[int(p * (len(xs) - 1))]  # noqa: E731
    return {"jobs": len(ids),
            "real_wait_s": {"median": q(rw, .5), "p90": q(rw, .9), "p99": q(rw, .99)},
            "sim_wait_s": {"median": q(sw, .5), "p90": q(sw, .9), "p99": q(sw, .99)}}


WORKSPACE_MB = (0, 8192)


def run_replay(args):
    # The platform admits CPU against *measured* busy cores, which a trace
    # does not record; GPU jobs rarely saturate the host's cores, so CPU is
    # taken as non-binding here and the cards are the constraint.
    base = Config(cores=1e9)
    trace, sessions = load_trace(args.trace)
    out = {"jobs": len(trace), "sessions": len(sessions),
           "users": sorted({j.user for j in trace} | {w.user for w in sessions}),
           "validation": {}, "by_workspace_mb": {}}
    for ws_mb in WORKSPACE_MB:
        cfg = dataclasses.replace(base, workspace_mb=ws_mb)
        out["validation"][ws_mb] = validate(trace, sessions if ws_mb else [], cfg)
        print("validation", ws_mb, out["validation"][ws_mb], flush=True)
        out["by_workspace_mb"][ws_mb] = {}
        for factor in (1, 4, 16):
            out["by_workspace_mb"][ws_mb][factor] = {}
            ws = compress_sessions(sessions, factor) if ws_mb else []
            for policy in POLICIES:
                m = metrics(simulate(compress(trace, factor), cfg, policy, sessions=ws), cfg)
                out["by_workspace_mb"][ws_mb][factor][policy] = m
                print(ws_mb, factor, policy, {k: (round(m[k], 3) if isinstance(m[k], float) else m[k])
                                              for k in KEYS}, flush=True)
    return {"config": dataclasses.asdict(base), **out}


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


CARDS = {"48GiB": 49140, "24GiB": 24564}


def run_vram(args):
    """How the size of VRAM requests changes waiting and utilisation (trace).

    Run on the cards the deployment has and on a stock 24 GiB card.  On the
    smaller card a job whose request, or whose real holding, does not fit
    could not have run at all; those are counted and left out.
    """
    trace, _ = load_trace(args.trace)
    variants = {
        "as_requested": lambda j: j.request_mb,
        "right_sized": lambda j: max(512, int(j.actual_mb)),      # oracle: request = held
        "inflated_x1.5": lambda j: int(j.request_mb * 1.5),
        "deflated_x0.5": lambda j: max(512, int(j.request_mb * 0.5)),
    }
    out = {}
    for card, vram in CARDS.items():
        cfg = Config(cores=1e9, vram_mb=vram)          # cores as in run_replay
        fits = [j for j in trace if not j.gpu_count
                or (j.request_mb <= vram - cfg.headroom_mb and j.actual_mb <= vram - cfg.headroom_mb)]
        out[card] = {"vram_mb": vram, "jobs": len(fits), "excluded_do_not_fit": len(trace) - len(fits),
                     "by_factor": {}}
        for factor in (1, 2, 4, 8, 16):
            out[card]["by_factor"][factor] = {}
            for name, fn in variants.items():
                jobs = [dataclasses.replace(j, request_mb=min(vram - cfg.headroom_mb, fn(j)) if j.gpu_count else 0)
                        for j in compress(fits, factor)]
                m = metrics(simulate(jobs, cfg, "platform"), cfg)
                # jobs whose real holding exceeds their allowance would be stopped
                m["would_be_stopped"] = sum(1 for j in jobs if j.gpu_count and j.actual_mb > allowance(cfg, j.request_mb))
                m.pop("per_user", None)
                out[card]["by_factor"][factor][name] = m
                print(card, factor, name, {k: (round(m[k], 3) if isinstance(m[k], float) else m[k])
                                           for k in KEYS + ("vram_held_over_reserved", "would_be_stopped")},
                      flush=True)
    return {"by_card": out}


def run_export(args):
    exclude = {u for u in args.exclude_users.split(",") if u}
    return export_trace(args.db, args.until, exclude)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("experiment",
                    choices=["export-trace", "synthetic", "interactive", "replay", "sweep", "vram"])
    ap.add_argument("--db", default="data/gpu_platform.db", help="platform database (export-trace)")
    ap.add_argument("--until", default="2026-10-21", help="last day exported (export-trace)")
    ap.add_argument("--exclude-users", default="admin,demouser,expa,expb,expc,expw",
                    help="accounts left out of the trace: administrator, demo, experiments")
    ap.add_argument("--trace", default="scripts/experiments/results/trace.json")
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--cap", type=int, default=2, help="jobs running per user (synthetic)")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    result = {"export-trace": run_export, "synthetic": run_synthetic,
              "interactive": run_interactive, "replay": run_replay,
              "sweep": run_sweep, "vram": run_vram}[args.experiment](args)
    result["generated"] = datetime.now().astimezone().isoformat(timespec="seconds")
    with open(args.out, "w") as fh:
        json.dump(result, fh, indent=1, default=str)


if __name__ == "__main__":
    main()
