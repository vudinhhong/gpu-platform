"""Fair-share ordering for the job queue.

First-come-first-served is the wrong rule for shared hardware.  If one user
submits ten jobs and another submits one a minute later, FIFO makes the second
user wait for all ten, the person who asked for the most gets served first,
which is exactly backwards.

Instead each user carries a **score**: what they have been given recently, plus
what they hold right now.  The queue serves the lowest score first, so the ten
jobs and the one job interleave, and a user who has been idle overtakes one who
has been running all morning.

The score is in GPU-seconds.  CPU time is converted with
``FAIRSHARE_CPU_CORE_WEIGHT`` so a CPU-only job still counts: it occupies real
capacity, and ignoring it would let someone monopolise the machine with work
that happens not to touch a GPU.
"""

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict

import models
from config import settings

logger = logging.getLogger(__name__)


def _weighted(gpu_seconds: float, cpu_seconds: float) -> float:
    """GPU-seconds plus CPU-seconds converted to the same unit."""
    return (gpu_seconds or 0.0) + (cpu_seconds or 0.0) * settings.FAIRSHARE_CPU_CORE_WEIGHT


def historical_usage(db, now: datetime = None) -> Dict[int, float]:
    """Recent usage per user, decayed so old consumption fades.

    Without the decay a user who ran a big job last week would stay at the back
    of the queue indefinitely; with it, the queue reflects who has been using
    the machine *lately*.
    """
    now = now or datetime.utcnow()
    since = now - timedelta(hours=settings.FAIRSHARE_WINDOW_HOURS)
    half_life = max(0.1, settings.FAIRSHARE_HALFLIFE_HOURS)

    scores: Dict[int, float] = defaultdict(float)
    records = (
        db.query(models.UsageRecord)
        .filter(models.UsageRecord.started_at >= since)
        .all()
    )
    for record in records:
        if record.user_id is None:
            continue
        ended = record.ended_at or now
        age_hours = max(0.0, (now - ended).total_seconds() / 3600.0)
        decay = 0.5 ** (age_hours / half_life)
        scores[record.user_id] += _weighted(record.gpu_seconds, record.cpu_seconds) * decay

    # Work that is still running has not been booked yet; count it at its
    # current elapsed time so a long-running job is felt while it runs, not
    # only once it ends.
    for record in records:
        if record.ended_at is not None or record.user_id is None:
            continue
        elapsed = max(0.0, (now - max(record.started_at, since)).total_seconds())
        scores[record.user_id] += _weighted(
            elapsed * max(1, record.gpu_count or 0) if record.gpu_count else 0.0,
            elapsed * (record.cpu_cores or 0.0),
        )
    return scores


def current_allocation(db) -> Dict[int, float]:
    """What each user is holding right now, as a forward-looking charge.

    A job that just started has consumed almost nothing, yet it occupies a GPU.
    Charging it as if it will run for ``FAIRSHARE_LOOKAHEAD_SECONDS`` is what
    makes the scheduler move to the next user after admitting one job instead
    of filling every slot with the same person's backlog.
    """
    lookahead = settings.FAIRSHARE_LOOKAHEAD_SECONDS
    scores: Dict[int, float] = defaultdict(float)

    active = (models.JobStatus.starting, models.JobStatus.running)
    for job in db.query(models.Job).filter(models.Job.status.in_(active)).all():
        scores[job.user_id] += _weighted(
            (job.gpu_count or 0) * lookahead,
            (job.cpu_cores or 0.0) * lookahead,
        )

    # Interactive workspaces hold CPU (and a GPU, if assigned) for as long as
    # they are open, so they belong in the same accounting.
    for session in db.query(models.JupyterSession).filter(
        models.JupyterSession.status == models.SessionStatus.running
    ).all():
        user = session.user
        if user is None:
            continue
        assignment = user.gpu_assignment
        gpus = len([
            i for i in (assignment.gpu_indices if assignment else "").split(",") if i.strip()
        ])
        cores = float((assignment.cpu_cores if assignment else None)
                      or settings.DEFAULT_CPU_CORES or 0)
        scores[user.id] += _weighted(gpus * lookahead, cores * lookahead)

    return scores


def scores(db) -> Dict[int, float]:
    """Combined fair-share score per user id; lower is served first."""
    now = datetime.utcnow()
    combined: Dict[int, float] = defaultdict(float)
    for source in (historical_usage(db, now), current_allocation(db)):
        for user_id, value in source.items():
            combined[user_id] += value
    return combined


def charge(scores_map: Dict[int, float], user_id: int, gpu_count: int, cpu_cores: float) -> None:
    """Add a just-admitted job to a user's score, in place.

    Called as the scheduler admits jobs within one pass so the next pick moves
    on to someone else, the interleaving happens here.
    """
    lookahead = settings.FAIRSHARE_LOOKAHEAD_SECONDS
    scores_map[user_id] = scores_map.get(user_id, 0.0) + _weighted(
        (gpu_count or 0) * lookahead, (cpu_cores or 0.0) * lookahead
    )


def explain(db) -> Dict[str, Dict[str, float]]:
    """Per-username breakdown, for the admin view and for debugging order."""
    history = historical_usage(db)
    current = current_allocation(db)
    users = {u.id: u.username for u in db.query(models.User).all()}
    out = {}
    for user_id, username in users.items():
        recent = round(history.get(user_id, 0.0), 1)
        holding = round(current.get(user_id, 0.0), 1)
        if recent or holding:
            out[username] = {
                "recent_usage": recent,
                "holding_now": holding,
                "score": round(recent + holding, 1),
            }
    return out
