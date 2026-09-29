"""Per-user budgets: disk space, and GPU / CPU hours spent on batch jobs.

Assignments answered *which* GPUs a user may touch but never *how much*, one
person could hold both GPUs for a month and fill the data disk, and the
platform had no opinion about it.

**The time budgets cover the queue, not the workspace.**  A workspace is
somebody's own seat at the machine: they opened it, they are sitting in front
of it, and an assignment already bounds what it may hold at once.  The queue is
the opposite, work submitted in bulk and left to run unattended, where one
person with a loop that submits fifty jobs takes the machine from everybody
else without ever meaning to.  That is what a budget is for, so it is charged
to jobs only and an interactive session costs nothing.  (An idle workspace is a
real problem too, but it is the idle reaper's, not the budget's:
``IDLE_TIMEOUT_MINUTES`` returns a session nobody is using.)

A budget also has to be answerable by the person who is over it.  Disk and time
differ there: files can be deleted, hours cannot be given back.  Being over
disk costs a user the GPU and the queue but never their workspace, which is
where the files are; being out of hours costs them the queue alone.

Job time is counted the way a batch scheduler counts it, wall-clock multiplied
by what was allocated: GPU-hours are hours x cards held, CPU-hours are hours x
cores allocated.  Holding a card or a core is what denies it to somebody else,
whether or not the job is doing anything with it.  The budget refills at the
start of every period (``QUOTA_PERIOD``, a week by default), which is short
enough that a bad estimate costs days rather than the rest of the month.
"""

import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

import models
from config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The budget period
# ---------------------------------------------------------------------------

def _offset() -> timedelta:
    """Local time minus UTC, for deciding where a period starts."""
    try:
        return timedelta(hours=float(settings.QUOTA_TZ_OFFSET_HOURS or 0.0))
    except (TypeError, ValueError):
        return timedelta(0)


def period_kind() -> str:
    return "month" if (settings.QUOTA_PERIOD or "week").lower() == "month" else "week"


def period_bounds(now: Optional[datetime] = None) -> Tuple[datetime, datetime]:
    """``(start, end)`` of the period *now* falls in, both UTC.

    The arithmetic is done in the operator's local time and converted back, so
    a weekly budget refills at local midnight on Monday rather than at 07:00 on
    a Monday morning in Hanoi.
    """
    now = now or datetime.utcnow()
    offset = _offset()
    local = now + offset
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)

    if period_kind() == "month":
        start = midnight.replace(day=1)
        end = (start + timedelta(days=32)).replace(day=1)
    else:
        start = midnight - timedelta(days=local.weekday())  # back to Monday
        end = start + timedelta(days=7)
    return start - offset, end - offset


def period_label(now: Optional[datetime] = None) -> str:
    """What to call this period on screen: ``2026-W39`` or ``2026-09``."""
    start, _ = period_bounds(now)
    local = start + _offset()
    if period_kind() == "month":
        return local.strftime("%Y-%m")
    year, week, _ = local.isocalendar()
    return f"{year}-W{week:02d}"


def period_reset_text(now: Optional[datetime] = None) -> str:
    """When the budget refills, phrased for the person waiting on it."""
    _, end = period_bounds(now)
    local = end + _offset()
    return local.strftime("%a %d %b at %H:%M")


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

def disk_quota_mb(user: "models.User") -> int:
    value = user.disk_quota_mb if user.disk_quota_mb is not None else settings.DEFAULT_DISK_QUOTA_MB
    return max(0, int(value or 0))


def gpu_hours_quota(user: "models.User") -> float:
    value = (
        user.gpu_hours_quota
        if user.gpu_hours_quota is not None
        else settings.DEFAULT_GPU_HOURS_QUOTA
    )
    return max(0.0, float(value or 0.0))


def cpu_hours_quota(user: "models.User") -> float:
    value = (
        getattr(user, "cpu_hours_quota", None)
        if getattr(user, "cpu_hours_quota", None) is not None
        else settings.DEFAULT_CPU_HOURS_QUOTA
    )
    return max(0.0, float(value or 0.0))


# ---------------------------------------------------------------------------
# What has been used
# ---------------------------------------------------------------------------

#: Ledger rows written by the job runner.  Interactive sessions write
#: "container" or "process" and are deliberately not counted; see the module
#: docstring.
JOB_BACKEND = "job"


def hours_used(db, user: "models.User", now: Optional[datetime] = None) -> Dict[str, float]:
    """GPU-hours and CPU core-hours this user's JOBS have spent this period.

    Two sources, because a job only writes its ledger row when it ends:

    * finished jobs, from ``usage_records``;
    * jobs on the machine right now, counted up to this moment, which is what
      stops a three-day run from outrunning the budget it started inside.

    Either way only the part that falls inside the period counts.  That matters
    once periods are a week long: a job that starts on Sunday evening and ends
    on Monday belongs to both weeks, and charging all of it to whichever week it
    started in would either hand out a free night or bill for one twice.
    """
    from sqlalchemy import or_

    now = now or datetime.utcnow()
    start, end = period_bounds(now)
    horizon = min(end, now)

    gpu_seconds = 0.0
    cpu_seconds = 0.0

    def charge(began, finished, gpu_count, cores) -> None:
        nonlocal gpu_seconds, cpu_seconds
        seconds = max(0.0, (min(finished, horizon) - max(began, start)).total_seconds())
        gpu_seconds += seconds * max(0, gpu_count or 0)
        cpu_seconds += seconds * max(0.0, cores or 0.0)

    rows = (
        db.query(models.UsageRecord)
        .filter(
            models.UsageRecord.user_id == user.id,
            models.UsageRecord.backend == JOB_BACKEND,
            models.UsageRecord.started_at < horizon,
            or_(
                models.UsageRecord.ended_at.is_(None),
                models.UsageRecord.ended_at > start,
            ),
        )
        .all()
    )
    for row in rows:
        charge(row.started_at, row.ended_at or horizon, row.gpu_count, row.cpu_cores)

    live = (
        db.query(models.Job)
        .filter(
            models.Job.user_id == user.id,
            models.Job.status.in_((models.JobStatus.starting, models.JobStatus.running)),
            models.Job.started_at.isnot(None),
        )
        .all()
    )
    for job in live:
        charge(job.started_at, horizon, job.gpu_count, job.cpu_cores)

    return {
        "gpu_hours": round(gpu_seconds / 3600.0, 2),
        "cpu_hours": round(cpu_seconds / 3600.0, 2),
    }


def disk_used_mb(username: str, user=None, max_age: Optional[float] = None) -> int:
    """Megabytes this user's workspace occupies.

    Measured at the workspace's real location: a mapped home is not under
    JUPYTER_DATA_DIR, and the cached scan of that directory would report zero
    for it forever.

    *max_age* caps how old a cached measurement may be.  Left unset it accepts
    whatever the periodic scan last recorded, which is what the dashboard
    wants; a caller about to refuse someone passes a short one.
    """
    from services import metrics, workspaces

    if user is not None and (user.home_path or "").strip():
        return metrics.directory_size_mb(
            workspaces.for_user(user).backend_path, max_age=max_age)
    if max_age is None:
        return int(metrics.user_disk_usage().get(username, 0))
    # Re-measure this one workspace rather than rerunning the bulk scan: the
    # person waiting to submit should not pay for every other user's files.
    return metrics.set_user_disk_usage(username, metrics.directory_size_mb(
        os.path.join(settings.JUPYTER_DATA_DIR, username), max_age=max_age))


# ---------------------------------------------------------------------------
# Standings
# ---------------------------------------------------------------------------

def _meter(used: float, limit: float, label: str) -> Dict[str, Any]:
    return {
        "used": used,
        "quota": limit or None,
        "percent": round(used / limit * 100, 1) if limit else None,
        "over": bool(limit and used >= limit),
        "period": label,
    }


def time_snapshot(db, user: "models.User") -> Dict[str, Any]:
    """The two time budgets, with no disk measurement attached.

    Separate from :func:`snapshot` because the callers that run on a timer,
    the mid-session enforcement pass and the job scheduler, need this many
    times a minute, and the disk figure behind it costs a ``du`` of the whole
    workspace.
    """
    used = hours_used(db, user)
    label = period_label()
    _, end = period_bounds()
    return {
        "period": {
            "kind": period_kind(),
            "label": label,
            "resets_at": end.isoformat(),
            "resets_text": period_reset_text(),
        },
        "gpu_hours": _meter(used["gpu_hours"], gpu_hours_quota(user), label),
        "cpu_hours": _meter(used["cpu_hours"], cpu_hours_quota(user), label),
    }


def snapshot(db, user: "models.User") -> Dict[str, Any]:
    """Everything the UI needs to show a user their standing."""
    disk_limit = disk_quota_mb(user)
    disk_used = disk_used_mb(user.username, user)

    # A cached figure is fine while there is room, but it must not be what
    # refuses somebody.  Deleting files is the only way back under the budget,
    # and until the scan expired the platform kept quoting the size from
    # before the deletion, so the user freed space, tried again, and was told
    # the same thing, while `limits` inside their workspace measured live and
    # disagreed.  Confirm with a fresh look before the number costs anything.
    if disk_limit and disk_used > disk_limit:
        disk_used = disk_used_mb(user.username, user,
                                 max_age=settings.DISK_USAGE_RECHECK_SECONDS)

    state = time_snapshot(db, user)
    state["disk"] = {
        "used_mb": disk_used,
        "quota_mb": disk_limit or None,
        "percent": round(disk_used / disk_limit * 100, 1) if disk_limit else None,
        "over": bool(disk_limit and disk_used > disk_limit),
    }
    return state


def _gb(mb: Optional[int]) -> str:
    """Megabytes the way a person reads them."""
    if not mb:
        return "0 MB"
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{int(mb)} MB"


# ---------------------------------------------------------------------------
# Who may run what
# ---------------------------------------------------------------------------

def time_blocker(state: Dict[str, Any]) -> Optional[str]:
    """Why this user may not run a job right now, or None.

    One sentence, addressed to the user, and the same sentence wherever it
    lands: the 429 that refuses a submission, the line `queue` prints under a
    job that is waiting, and the note on a job that was put back in the queue.
    A workspace is never refused for this; the budget is the queue's.
    """
    period = state.get("period") or {}
    resets = period.get("resets_text")
    for key, noun in (("gpu_hours", "GPU hours"), ("cpu_hours", "CPU hours")):
        meter = state.get(key) or {}
        if meter.get("over"):
            return (
                f"You have used all your {noun} for {meter.get('period')}: "
                f"{meter.get('used')}h of {meter.get('quota')}h. "
                f"The budget refills on {resets}"
                + (", or an administrator can raise it." if resets
                   else ". Ask an administrator to raise it.")
            )
    return None


def disk_blocker(state: Dict[str, Any]) -> Optional[str]:
    """Why this user may not start work that writes output, or None."""
    disk = state.get("disk") or {}
    if not disk.get("over"):
        return None
    return (
        f"Your workspace is using {_gb(disk.get('used_mb'))} of its "
        f"{_gb(disk.get('quota_mb'))} of space. Free some before running a job, "
        "since a job only adds more output."
    )


def job_blocker(db, user: "models.User", state: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Why a job of this user's may not run right now, or None.

    Used by the scheduler, which holds the job in the queue rather than failing
    it: every reason here is one that goes away on its own, when the period
    refills or when the user deletes something.
    """
    state = state if state is not None else snapshot(db, user)
    return time_blocker(state) or disk_blocker(state)


def workspace_state(db, user: "models.User") -> Dict[str, Any]:
    """The standing a workspace start is decided against.  Refuses nothing.

    Nothing here can stop a workspace: not the disk budget, because the
    workspace is the only way the user can reach their files and refusing it
    left them with a quota they had no means to get back under, the error
    telling them to delete files while denying them the one place they could
    delete files from; and not the hour budgets, because those are the queue's
    and a workspace is the user's own seat at the machine.

    Being over disk still costs the GPU and the job queue, which the caller
    applies from this state: the user can clean up, they just cannot compute
    until they have.
    """
    return snapshot(db, user)


def over_disk_message(state: Dict[str, Any]) -> str:
    """What to tell a user who is over their disk budget."""
    disk = state["disk"]
    over_by = max(0, int(disk["used_mb"] or 0) - int(disk["quota_mb"] or 0))
    return (
        f"Your workspace is using {_gb(disk['used_mb'])} of its "
        f"{_gb(disk['quota_mb'])} of space, {_gb(over_by)} over. "
        "It has started without a GPU and cannot run jobs until you free some "
        "space; delete what you no longer need and start it again."
    )


def assert_can_submit_job(db, user: "models.User") -> None:
    """Raise HTTP 429 when nothing this user submits could run.

    This is where the budgets bite, and the only place a user is turned away
    for being out of hours: a job is work handed to the machine to run
    unattended, and one person's backlog is what takes the queue from everybody
    else.  The disk budget applies too, since a job only adds more output.
    """
    from fastapi import HTTPException, status

    reason = job_blocker(db, user)
    if reason:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=reason,
        )
