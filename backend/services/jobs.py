"""Batch job queue: run a user's shell script on a GPU when one has room.

Why this exists
---------------
GPU assignments are static: a user either holds a card or they do not, and an
idle card stays idle while someone without an assignment has nothing to run on.
A job queue turns the same hardware into a shared resource, anyone may submit,
and the scheduler starts the work when a GPU can actually take it.

Guarantees
----------
* A job runs in its own container built exactly like its owner's interactive
  workspace: same image, same workspace mount, same account, same memory / CPU /
  process / disk-I/O caps.  It cannot exceed what its owner may use and cannot
  see another user's files.
* Placement is by free VRAM, so a job that needs 4 GB can share a card that
  still has 20 GB free rather than waiting for the whole card.
* A running job's request is treated as a reservation for its lifetime, so two
  jobs are never admitted into the same free space.
"""

import hashlib
import logging
import os
import re
import secrets
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, status

import models
from config import settings

logger = logging.getLogger(__name__)

_ACTIVE = (models.JobStatus.queued, models.JobStatus.starting,
           models.JobStatus.running, models.JobStatus.paused)
#: On the machine and holding hardware.  A paused job is deliberately not here:
#: its processes are frozen, so it occupies no GPU and no cores, and it is
#: charged for none of the time it spends this way.
_ON_GPU = (models.JobStatus.starting, models.JobStatus.running)


# ---------------------------------------------------------------------------
# Workspace paths, every path a user supplies has to be proven safe
# ---------------------------------------------------------------------------

def workspace_root(username: str) -> str:
    """Where this user's files live, which may be a directory that predates
    the platform, if an administrator mapped one."""
    from database import SessionLocal
    from services import workspaces

    db = SessionLocal()
    try:
        user = db.query(models.User).filter(models.User.username == username).first()
        if user is not None:
            return workspaces.for_user(user).backend_path
    except Exception:  # noqa: BLE001 (never fail a path lookup on the DB)
        pass
    finally:
        db.close()
    return os.path.join(settings.JUPYTER_DATA_DIR, username)


def resolve_in_workspace(username: str, relative: str) -> str:
    """Resolve *relative* inside the user's workspace, or raise HTTP 400.

    A job runs as its owner with their workspace mounted, so a path like
    ``../other-user/secret.sh`` would not read another user's files anyway, but
    it would let someone point a job at a path that means nothing in the
    container.  Resolving and containment-checking here keeps the error at
    submit time, when the user can still do something about it.
    """
    root = os.path.realpath(workspace_root(username))
    candidate = os.path.realpath(os.path.join(root, (relative or "").lstrip("/")))
    if candidate != root and not candidate.startswith(root + os.sep):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That path is outside your workspace.",
        )
    return candidate


def to_relative(username: str, absolute: str) -> str:
    root = os.path.realpath(workspace_root(username))
    return os.path.relpath(absolute, root).replace("\\", "/")


# ---------------------------------------------------------------------------
# Job tokens, how the in-workspace commands authenticate
# ---------------------------------------------------------------------------

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def ensure_job_token(db, user: "models.User") -> str:
    """Return a fresh job token for *user* and publish it into their workspace.

    Written as a file the account owns rather than handed out on screen: the
    commands read it themselves, so the user never has to know it exists.
    """
    from services import container_manager

    token = secrets.token_urlsafe(32)
    user.job_token_hash = _hash_token(token)
    db.add(user)
    db.commit()
    container_manager.write_job_token(user.username, token)
    return token


def user_for_token(db, token: str) -> Optional["models.User"]:
    if not token:
        return None
    return (
        db.query(models.User)
        .filter(
            models.User.job_token_hash == _hash_token(token),
            models.User.deleted_at.is_(None),
        )
        .first()
    )


# ---------------------------------------------------------------------------
# Request bounds, the caller is not necessarily the CLI
# ---------------------------------------------------------------------------
#
# submit/queue/cancel check their arguments before sending them, but those
# checks belong to the client and a job token is enough to skip it: the
# commands are readable, the token is the user's own file, and the API answers
# a hand-written request just the same.  So every number a caller supplies is
# bounded again here, where it cannot be bypassed.

def _as_int(value, field: str, default: int = 0) -> int:
    """Parse a caller-supplied number, or refuse it with a usable message."""
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field} has to be a whole number.",
        )


def _bounded_runtime(requested) -> Optional[int]:
    """The run-time limit for a job, with the platform's ceiling applied.

    A request that would outlive the ceiling is refused rather than quietly
    trimmed: nobody should plan a twelve-hour run around a limit that was
    going to stop it after one.
    """
    cap = settings.JOB_MAX_RUNTIME_MINUTES or 0
    minutes = _as_int(requested, "--max-minutes", default=0)
    if minutes <= 0:
        # Nothing asked for: the platform ceiling, or no limit if there is none.
        return cap or None
    if cap and minutes > cap:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Jobs on this platform may run for at most {cap} minutes.",
        )
    return minutes


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------

def submit(
    db,
    user: "models.User",
    script: str,
    workdir: str = "",
    name: Optional[str] = None,
    gpu_count: Optional[int] = None,
    gpu_memory_mb: Optional[int] = None,
    max_runtime_minutes: Optional[int] = None,
) -> "models.Job":
    """Validate and queue a job.  Raises HTTPException with a usable message."""
    if not settings.JOBS_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Job submission is not available on this platform.",
        )

    from services import quota

    # Same gate as starting a workspace: out of hours or over the disk budget
    # means no.  Refused here rather than queued and held, so somebody typing
    # `submit` learns at once that nothing will run and when that changes; the
    # scheduler holds jobs that were already accepted, which is a different
    # question from whether to accept more.
    quota.assert_can_submit_job(db, user)

    queued = (
        db.query(models.Job)
        .filter(models.Job.user_id == user.id, models.Job.status == models.JobStatus.queued)
        .count()
    )
    if queued >= settings.JOB_MAX_QUEUED_PER_USER:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"You already have {queued} jobs waiting. Let some finish first.",
        )

    script_abs = resolve_in_workspace(user.username, script)
    if not os.path.isfile(script_abs):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Script not found: {script}",
        )

    workdir_abs = resolve_in_workspace(user.username, workdir or os.path.dirname(script))
    if not os.path.isdir(workdir_abs):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Directory not found: {workdir}",
        )

    # ── What the job asks of the GPUs ────────────────────────────────────
    #
    # The memory figure is the switch, not a detail of it: a job that names no
    # VRAM is a CPU job, and a job that names some is a GPU job.  There is no
    # default any more.  The platform used to supply one, which meant the most
    # common way to get a reservation wrong was to say nothing at all, the
    # scheduler would fit a neighbour into 4 GB that was never really free,
    # and the neighbour was the one that died of it.  A number nobody chose is
    # worse than no number, because it looks like a decision.
    memory = _as_int(gpu_memory_mb, "--gpu-memory", default=0)
    if memory < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="--gpu-memory cannot be negative.",
        )

    # None = "not mentioned"; 0 = "--no-gpu".  The two have to stay apart:
    # the first is the ordinary CPU job, the second contradicts --gpu-memory.
    not_mentioned = gpu_count is None or gpu_count == ""
    requested_gpus = None if not_mentioned else max(0, _as_int(gpu_count, "--gpus"))

    if memory == 0:
        if requested_gpus:
            # Worded without assuming which flag was typed: a workspace still
            # running the previous `submit` sends a GPU count of its own
            # accord, and telling that user about a --gpus they never typed
            # would send them looking for the wrong thing.
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "This job asks for a GPU but not for an amount of GPU memory. "
                    "Add --gpu-memory MB (for example --gpu-memory 20000). The "
                    "scheduler fits other work around that figure, so it has to "
                    "be stated. Without a GPU at all, the job runs on CPU."
                ),
            )
        gpu_count = 0
    else:
        if requested_gpus == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="--no-gpu and --gpu-memory contradict each other. Drop one of them.",
            )
        gpu_count = requested_gpus or 1

        # An empty pool means the GPU monitor had nothing to say, not that
        # there are no cards; only refuse on a count we know is impossible.
        pool = _pool_gpus()
        if pool and gpu_count > len(pool):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"There are {len(pool)} GPU(s) here, so a job asking for "
                    f"{gpu_count} would wait forever."
                ),
            )
        largest = max((g["total_memory_mb"] for g in pool), default=0)
        if largest and memory > largest:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"No GPU here has {memory} MB of memory. The largest has "
                    f"{largest} MB. Lower the request or the job would wait forever."
                ),
            )

    job = models.Job(
        user_id=user.id,
        username=user.username,
        name=(name or os.path.basename(script))[:128],
        script=to_relative(user.username, script_abs),
        workdir=to_relative(user.username, workdir_abs),
        gpu_count=gpu_count,
        gpu_memory_mb=memory,
        max_runtime_minutes=_bounded_runtime(max_runtime_minutes),
        status=models.JobStatus.queued,
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    # The id is only known now, and the output file is named after it.
    job.output_path = os.path.join(job.workdir, f"output.{job.id}.out").lstrip("./")
    db.add(job)
    db.commit()
    db.refresh(job)

    from services import audit

    audit.record(
        db, "job.submit", actor=user.username, target=f"job:{job.id}",
        detail=f"script={job.script} gpus={gpu_count}x{memory}MB",
    )
    logger.info("Queued job %d for %r: %s", job.id, user.username, job.script)
    return job


def cancel(db, user: "models.User", job_ids: List[int], by_admin: bool = False) -> Dict[str, Any]:
    """Cancel jobs the caller owns (or any job, for an admin)."""
    from services import audit, container_manager

    cancelled, skipped = [], []
    query = db.query(models.Job).filter(models.Job.id.in_(job_ids))
    if not by_admin:
        query = query.filter(models.Job.user_id == user.id)
    found = {job.id: job for job in query.all()}

    for job_id in job_ids:
        job = found.get(job_id)
        if job is None:
            skipped.append({"id": job_id, "reason": "not found"})
            continue
        if not job.is_active:
            skipped.append({"id": job_id, "reason": f"already {job.status.value}"})
            continue
        if job.container_id:
            container_manager.stop_job_container(job.id)
        job.status = models.JobStatus.cancelled
        job.finished_at = datetime.utcnow()
        job.message = "Cancelled by an administrator." if by_admin else "Cancelled."
        db.add(job)
        cancelled.append(job_id)

    if cancelled:
        audit.record(
            db, "job.cancel", actor=user.username,
            detail=f"jobs={cancelled}", commit=False,
        )
        db.commit()
    return {"cancelled": cancelled, "skipped": skipped}


# ---------------------------------------------------------------------------
# GPU accounting
# ---------------------------------------------------------------------------

def _pool_gpus() -> List[Dict[str, Any]]:
    """GPUs the queue may place jobs on.

    None of them while the GPU reading is stale.  Placement is arithmetic on
    free VRAM, and doing that arithmetic on a figure that stopped updating
    half an hour ago is how two jobs end up in the same space; waiting is the
    cheaper mistake, and `queued_blockers` says out loud that this is why.
    """
    from services import gpu_monitor

    gpus = gpu_monitor.get_gpu_status()
    if not gpu_monitor.telemetry()["ok"]:
        return []
    raw = (settings.JOB_GPU_POOL or "").strip()
    if not raw:
        return gpus
    allowed = {int(x) for x in raw.split(",") if x.strip().lstrip("-").isdigit()}
    return [g for g in gpus if g["index"] in allowed]


def gpu_availability(db) -> List[Dict[str, Any]]:
    """Free VRAM per GPU, counting what running jobs may still take.

    Two numbers matter and neither alone is enough: what nvidia-smi reports as
    used (covers everything on the card, including work outside the platform)
    and what admitted jobs are entitled to (covers a job that has started but
    not yet allocated).  Taking the larger of the two is what stops the
    scheduler from admitting two jobs into the same free space.

    The second number is the *allowance*, not the request.  A job holding its
    request plus a little is inside its allowance and is deliberately not
    stopped, so that margin is space the scheduler must not promise to anyone
    else: a job that has not allocated yet may legally grow into it later, and
    the job admitted beside it on the strength of the smaller figure is the one
    that would die of it.  It costs a sparser card -- up to the grace per job
    is held back -- which is the price of the grace being real.

    `reserved_mb` in the payload stays the sum of the requests, because that is
    the figure users chose and recognise; only placement uses the allowance.
    """
    reserved: Dict[int, int] = defaultdict(int)
    entitled: Dict[int, int] = defaultdict(int)
    running_count: Dict[int, int] = defaultdict(int)
    for job in db.query(models.Job).filter(models.Job.status.in_(_ON_GPU)).all():
        asked = job.gpu_memory_mb or 0
        for index in _parse_indices(job.gpu_indices):
            reserved[index] += asked
            entitled[index] += gpu_overrun_allowance_mb(asked) if asked else 0
            running_count[index] += 1

    out = []
    for gpu in _pool_gpus():
        index = gpu["index"]
        committed = max(gpu["used_memory_mb"], entitled[index])
        out.append({
            "index": index,
            "uuid": gpu.get("uuid"),
            "name": gpu["name"],
            "total_mb": gpu["total_memory_mb"],
            "used_mb": gpu["used_memory_mb"],
            "reserved_mb": reserved[index],
            # What those jobs may hold before being stopped, which is what the
            # free figure below is actually computed against.
            "entitled_mb": entitled[index],
            "free_mb": max(0, gpu["total_memory_mb"] - committed - settings.JOB_GPU_HEADROOM_MB),
            "running_jobs": running_count[index],
        })
    return out


def _allocated_cores(db) -> float:
    """Every core the platform has promised, used or not.

    The pessimistic reading, kept for when nothing has measured the machine.
    """
    total = 0.0
    for job in db.query(models.Job).filter(models.Job.status.in_(_ON_GPU)).all():
        total += job.cpu_cores or 0.0
    for session in db.query(models.JupyterSession).filter(
        models.JupyterSession.status == models.SessionStatus.running
    ).all():
        user = session.user
        assignment = user.gpu_assignment if user else None
        total += float((assignment.cpu_cores if assignment else None)
                       or settings.DEFAULT_CPU_CORES or 0)
    return total


def _busy_cores() -> Optional[float]:
    """Cores actually in use on this machine, or None if nothing measured it.

    Read from the host figure rather than by adding the containers up: a build,
    somebody's SSH session, anything outside the platform competes for the same
    cores and has to count against what the queue may hand out.
    """
    from services import metrics

    snap = metrics.latest()
    host = snap.get("host") or {}
    collected = snap.get("collected_at")
    percent = host.get("cpu_percent")
    cores = host.get("cpu_count")
    if collected is None or percent is None or not cores:
        return None
    if time.time() - collected > settings.JOB_CPU_MEASURE_MAX_AGE_SECONDS:
        return None
    return max(0.0, float(percent) / 100.0 * float(cores))


def _unramped_cores(db) -> float:
    """What a just-started job will use but has not started using yet.

    No measurement can see a job that is still importing torch.  Holding its
    allocation until it has had time to ramp is what stops the next pass, and
    the one after it, from admitting more work into that same idle reading.
    """
    cutoff = datetime.utcnow() - timedelta(seconds=settings.JOB_CPU_RAMP_SECONDS)
    total = 0.0
    for job in db.query(models.Job).filter(models.Job.status.in_(_ON_GPU)).all():
        if job.started_at is None or job.started_at > cutoff:
            total += job.cpu_cores or 0.0
    return total


def cpu_capacity(db) -> Dict[str, Any]:
    """Cores the queue may hand out, and how many are in use.

    A core count is a ceiling, not a reservation: the cgroup lets a workspace
    burst up to it and takes nothing back while it idles.  Adding those
    ceilings up therefore called a machine nobody was using full, three idle
    workspaces allocated 34 cores between them left a 32-core box with nothing
    for the queue, and jobs waited on CPU that was sitting there unused.

    So what is in use is measured instead.  The one thing the measurement
    cannot see is added back: a job admitted moments ago has not loaded a core
    yet, and without holding its allocation the scheduler would admit another
    into the same idle reading, and another, until the machine was swamped.

    A job is still capped at its owner's allocation once it runs, that has not
    changed.  What changed is only who may start: an idle ceiling no longer
    reserves a core against everybody else.

    With no fresh measurement, just after a restart, or when docker stats is
    unavailable it falls back to adding the ceilings up, erring towards
    refusing work rather than flooding the box.
    """
    import os as _os

    total = float(settings.JOB_CPU_POOL_CORES or 0)
    if total <= 0:
        total = max(1.0, (_os.cpu_count() or 8) - float(settings.JOB_CPU_RESERVED_CORES or 0))

    busy = _busy_cores()
    if busy is None:
        used, basis = _allocated_cores(db), "allocated"
    else:
        used, basis = busy + _unramped_cores(db), "measured"

    return {
        "total_cores": round(total, 2),
        "committed_cores": round(used, 2),
        "free_cores": round(max(0.0, total - used), 2),
        # Which reading this is, so "why is my job waiting" has an answer.
        "basis": basis,
    }


def cores_for(user: "models.User") -> float:
    """Cores a job of this user's will be given (their workspace allocation)."""
    assignment = user.gpu_assignment
    return float((assignment.cpu_cores if assignment else None)
                 or settings.DEFAULT_CPU_CORES or 0)


def _parse_indices(value: Optional[str]) -> List[int]:
    return [int(x) for x in (value or "").split(",") if x.strip().lstrip("-").isdigit()]


def _place(job: "models.Job", availability: List[Dict[str, Any]]) -> Optional[List[int]]:
    """Pick GPUs for *job*, or None when nothing has room right now."""
    if job.gpu_count <= 0:
        return []
    fits = [g for g in availability if g["free_mb"] >= job.gpu_memory_mb]
    if len(fits) < job.gpu_count:
        return None
    # Densest fit first: leave the emptiest cards free for jobs that need them.
    fits.sort(key=lambda g: (g["free_mb"], g["index"]))
    return [g["index"] for g in fits[: job.gpu_count]]


# ---------------------------------------------------------------------------
# The scheduler pass
# ---------------------------------------------------------------------------

def gpu_guard_pass() -> Dict[str, Any]:
    """Sample VRAM and stop anything past its allowance.

    Split out of the scheduler pass and run far more often.  Sampling on the
    scheduler's ten-second cycle meant the reservation was checked ten seconds
    after a job could have blown through it, and placement -- which is what the
    scheduler pass is for -- gains nothing from running that often.  One
    nvidia-smi covers every job here, so the cost does not grow with the queue.
    """
    if not settings.JOBS_ENABLED:
        return {}
    from database import SessionLocal
    from services import gpu_scan

    # Hold a failing scan back to the scheduler's own interval: a wedged driver
    # answers slowly, and asking it once a second achieves nothing but the wait.
    gpu_scan.refresh(retry_after_failure=settings.JOB_SCHEDULER_INTERVAL)
    if not gpu_scan.usable():
        # Without the scan, reading a job costs a docker exec, and doing that
        # for every job once a second is the expense this loop exists to avoid.
        # The scheduler pass already checks the same jobs the same way on its
        # own ten-second cycle, so standing down here degrades the guard to
        # exactly what this was before the scan existed, and no further.
        return {"checked": 0, "stopped": [], "scan": "unusable"}

    db = SessionLocal()
    try:
        running = (
            db.query(models.Job)
            .filter(models.Job.status == models.JobStatus.running)
            .filter(models.Job.gpu_count > 0)
            .filter(models.Job.gpu_memory_mb > 0)
            .all()
        )
        stopped = []
        for job in running:
            if _check_gpu_overrun(db, job) == "stopped":
                stopped.append(job.id)
        db.commit()
        return {"checked": len(running), "stopped": stopped}
    except Exception as exc:  # noqa: BLE001 (the loop must survive anything)
        logger.error("GPU guard pass failed: %s", exc)
        db.rollback()
        return {}
    finally:
        db.close()


def scheduler_pass() -> Dict[str, Any]:
    """One full cycle: finalise what ended, then start what fits."""
    if not settings.JOBS_ENABLED:
        return {}
    from database import SessionLocal

    db = SessionLocal()
    try:
        finished = _finalise_running(db)
        resumed = _resume_paused(db)
        started = _dispatch_queued(db)
        return {"finished": finished, "resumed": resumed, "started": started}
    except Exception as exc:  # noqa: BLE001 (the loop must survive anything)
        logger.error("Scheduler pass failed: %s", exc)
        db.rollback()
        return {}
    finally:
        db.close()


def _finalise_running(db) -> List[Dict[str, Any]]:
    """Record the outcome of jobs whose container has exited or timed out."""
    from services import container_manager

    results = []
    # True when something was written that no entry in *results* covers, a
    # note on a job that is still running.  Without it the change would sit
    # uncommitted until some unrelated job happened to finish in the same pass.
    changed = False
    jobs = db.query(models.Job).filter(models.Job.status.in_(_ON_GPU)).all()
    for job in jobs:
        state = container_manager.job_container_state(job.id)

        if state is None:
            # The container is gone without us seeing it exit.  It may simply
            # not exist yet in the moment after creation.
            if job.started_at and datetime.utcnow() - job.started_at < timedelta(seconds=30):
                continue
            _close(db, job, models.JobStatus.failed, None, "The job stopped unexpectedly.")
            results.append({"id": job.id, "status": "failed"})
            continue

        if state["running"]:
            if job.status == models.JobStatus.starting:
                job.status = models.JobStatus.running
                db.add(job)
                changed = True

            # Either check can end the job, and either can merely annotate it.
            # Both outcomes have to reach the database.  A stop whose status
            # change is rolled back is the worse of the two: the container is
            # really gone, so the next pass finds nothing and reports that the
            # job "stopped unexpectedly", losing the one message that said
            # what to fix.
            # Any of these can take the job off the machine, and the rest
            # have nothing to say about a job that is no longer on it.
            left = False
            for check in (_check_time_budget, _check_output_size, _check_gpu_overrun):
                outcome = check(db, job)
                if outcome in ("stopped", "requeued", "paused"):
                    results.append({"id": job.id, "status": job.status.value})
                    left = True
                    break
                if outcome == "noted":
                    changed = True
            if left:
                continue

            limit = job.max_runtime_minutes
            if limit and job.started_at and \
                    _elapsed_seconds(job) > limit * 60:
                container_manager.stop_job_container(job.id)
                container_manager.remove_job_container(job.id)
                _close(db, job, models.JobStatus.timeout, None,
                       f"Stopped after reaching its {limit} minute limit.")
                results.append({"id": job.id, "status": "timeout"})
            continue

        exit_code = state.get("exit_code")
        if state.get("oom_killed"):
            _close(db, job, models.JobStatus.failed, exit_code,
                   "The job ran out of memory and was stopped.")
        elif exit_code == 0:
            _close(db, job, models.JobStatus.succeeded, 0, None)
        else:
            _close(db, job, models.JobStatus.failed, exit_code,
                   f"The script exited with code {exit_code}.")
        container_manager.remove_job_container(job.id)
        results.append({"id": job.id, "status": job.status.value})

    if results or changed:
        db.commit()
    return results


def job_gpu_memory_mb(job: "models.Job") -> Optional[int]:
    """VRAM this job's processes actually hold, or None if it cannot be told.

    The host-wide scan answers for every job at once and costs one nvidia-smi,
    so it is asked first and is what makes a one-second guard affordable.  The
    per-container exec stays as the fallback for a deployment whose backend is
    not in the host PID namespace, where the scan can see nothing.
    """
    from services import container_manager, gpu_scan

    if job.container_id:
        mb = gpu_scan.container_mb(job.container_id)
        if mb is not None:
            return mb

    try:
        container = container_manager._docker_client().containers.get(
            container_manager.job_container_name(job.id)
        )
    except Exception:  # noqa: BLE001 (container gone mid-scan)
        return None
    return container_manager.container_gpu_memory_mb(container)


def gpu_overrun_allowance_mb(requested_mb: int) -> int:
    """The most a job reserving *requested_mb* may actually hold.

    Two allowances, and the larger wins.  The proportional one covers a job
    that simply asked low.  The flat one covers what the job never chose: a
    CUDA context is several hundred MB per process before a single tensor
    exists, and nvidia-smi counts it, so a job asking for exactly what its
    model needs would be stopped for the runtime's own overhead.
    """
    factor = float(settings.JOB_GPU_OVERRUN_FACTOR or 1.0)
    grace = int(settings.JOB_GPU_OVERRUN_GRACE_MB or 0)
    return int(max(requested_mb * factor, requested_mb + grace))


# Enough of the VRAM note to recognise one this module wrote, including notes
# already in the database from earlier versions ("is using N MB of GPU memory
# after reserving").  It starts at "B" rather than "MiB" on purpose: the unit
# has been written MB and is now written MiB, and a job stopped before that
# change still carries the old wording and must still be recognised.
# `message` is shared with the output-size check and with the final word on a
# job that ended, so a note may only be cleared by whoever wrote it.
_VRAM_NOTE_MARKER = "B of GPU memory after"


def _check_gpu_overrun(db, job: "models.Job") -> Optional[str]:
    """Stop a job holding more VRAM than it reserved.

    The reservation is what the scheduler used to decide another job would fit
    beside this one.  Left unenforced it is an honour system with a reward for
    lying: asking for 4 GB and taking 30 gets you to the front of the queue,
    and the job admitted into the space you claimed not to need is the one
    that dies of it.  So the figure is held to.

    What is measured is memory *held*, which is the right measure even though
    part of it may be a framework's free-list rather than live tensors: a
    cached block is still checked out of the driver and still unavailable to
    the neighbour.  From the card's side there is no difference.
    """
    from services import audit, container_manager

    if not job.gpu_count or not job.gpu_memory_mb:
        return None

    used = job_gpu_memory_mb(job)
    if used is None:
        return None

    # Record it whether or not it is a problem: a job at 80% of its allowance
    # is the one worth telling its owner about, and that can only be shown if
    # the figure is kept while everything is still fine.  Small movements are
    # not worth a write every second.
    outcome = None
    if job.gpu_memory_used_mb is None or abs(job.gpu_memory_used_mb - used) >= 64:
        job.gpu_memory_used_mb = used
        db.add(job)
        outcome = "noted"

    # The high-water mark, which is the figure that still means something once
    # the job has ended.  The live one is whatever the last reading caught, and
    # a job that released its memory before exiting leaves a zero behind: true
    # at the instant it was taken, and useless to anyone asking afterwards what
    # the run actually needed.
    #
    # A zero is never a high-water mark.  Writing one would make a job that
    # asked for a card and never touched it indistinguishable, on screen, from
    # one measured at nothing: both would read "0.0 GB peak", which claims a
    # measurement where there was only an absence.  Left null, it renders as
    # the dash it deserves.
    if used > (job.gpu_memory_peak_mb or 0):
        job.gpu_memory_peak_mb = used
        db.add(job)
        outcome = "noted"

    allowed = gpu_overrun_allowance_mb(job.gpu_memory_mb)
    if used <= allowed:
        # Back inside its budget.  A warning written while it was over has to
        # go: an allocator that released its cache does not deserve to carry
        # "this job is holding 43 GiB" for the rest of the run, and a warning
        # nobody can make go away is one people learn to ignore.
        if job.message and _VRAM_NOTE_MARKER in job.message:
            job.message = None
            db.add(job)
            outcome = "noted"
        return outcome

    # A job submitted while the reservation was advisory is flagged, never
    # stopped: its owner read a document that said the figure was not enforced,
    # and a deployment is not an argument for killing a run that is already
    # twenty hours in.  Only jobs submitted since are held to it.
    enforcing = (
        bool(getattr(job, "gpu_memory_enforced", True))
        and (settings.JOB_GPU_OVERRUN_ACTION or "stop").lower() == "stop"
    )

    # Over budget, so the debounce above is set aside and the exact figure is
    # stored: what the note says, what the dashboard draws and what this was
    # judged against all have to be the same number.
    if job.gpu_memory_used_mb != used:
        job.gpu_memory_used_mb = used
        db.add(job)

    detail = (f"holding {used} MiB of GPU memory after asking for "
              f"{job.gpu_memory_mb} MiB")
    # Round the suggestion up to the next 512 MiB: handing back the exact peak
    # would put the next run one allocation away from the same stop.
    suggested = ((used + 511) // 512) * 512

    if not enforcing:
        # Rewritten every time the figure moves, not written once and left:
        # this note is a description of the present, and a stale one is worse
        # than none because it is read as current.
        note = (f"This job is {detail}. The scheduler fitted other work beside it "
                f"on that figure, so please resubmit with --gpu-memory {suggested}.")
        if job.message == note:
            return outcome
        job.message = note
        db.add(job)
        return "noted"

    container_manager.stop_job_container(job.id)
    container_manager.remove_job_container(job.id)
    _close(db, job, models.JobStatus.failed, None,
           f"Stopped: it was {detail}, and the scheduler had fitted other work "
           f"beside it on that figure. Run it again with --gpu-memory {suggested}.")
    audit.record(db, "job.gpu_overrun_stopped", actor="system",
                 target=f"job:{job.id}", detail=detail, commit=False)
    return "stopped"


def _check_time_budget(db, job: "models.Job") -> Optional[str]:
    """Stop a running job whose owner has just run out of GPU or CPU hours.

    Admission cannot cover this: a job admitted with twenty minutes of budget
    left would otherwise run for three days.  What happens next is
    ``JOB_TIME_QUOTA_ACTION``, and the default is to put the job back in the
    queue rather than fail it.  Nothing about the job is wrong, its owner is
    simply out of hours for now, and a queued job starts by itself once the
    period refills, without anybody having to remember to resubmit it.

    The price is that the script runs again from the beginning and rewrites its
    output file, so a long run should checkpoint.  Set the action to ``stop``
    for a deployment where re-running is worse than losing the queue slot.
    """
    from services import audit, container_manager, quota

    action = (settings.JOB_TIME_QUOTA_ACTION or "requeue").lower()
    if action in ("off", "none"):
        return None

    user = job.user
    if user is None:
        return None

    reason = quota.time_blocker(quota.time_snapshot(db, user))
    if not reason:
        return None

    if action == "warn":
        note = f"{reason} This job was left running."
        if job.message == note:
            return None
        job.message = note
        db.add(job)
        return "noted"

    # A job holding no GPU can be frozen instead of stopped: nothing is re-run,
    # nothing is lost, and the cores go back to the queue.  A GPU job cannot,
    # because a frozen container keeps its VRAM and the card would sit locked
    # up until the period refilled.
    cpu_action = (settings.JOB_TIME_QUOTA_CPU_ACTION or "pause").lower()
    if not job.gpu_count and cpu_action == "pause":
        if container_manager.pause_job_container(job.id):
            _pause(db, job, f"{reason} It is paused where it stands and picks up "
                            "from the same point when the budget refills.")
            audit.record(db, "job.budget_paused", actor="system",
                         target=f"job:{job.id}", detail=reason, commit=False)
            return "paused"
        logger.warning("Job %d could not be paused, stopping it instead", job.id)

    container_manager.stop_job_container(job.id)
    container_manager.remove_job_container(job.id)

    if action == "requeue":
        _requeue(db, job, f"{reason} It is back in the queue and starts again "
                          "on its own once the budget refills; it will run from "
                          "the beginning, so its output file is rewritten.")
        audit.record(db, "job.budget_requeued", actor="system",
                     target=f"job:{job.id}", detail=reason, commit=False)
        return "requeued"

    _close(db, job, models.JobStatus.failed, None, reason)
    audit.record(db, "job.budget_stopped", actor="system",
                 target=f"job:{job.id}", detail=reason, commit=False)
    return "stopped"


def _elapsed_seconds(job: "models.Job", now: Optional[datetime] = None) -> int:
    """How long this job has been running, across pauses.

    A paused job's clock is stopped, so what it shows is what it had run when
    it was frozen, which is also what its runtime limit is measured against.
    """
    total = float(job.runtime_seconds or 0.0)
    if job.started_at:
        total += max(0.0, ((now or datetime.utcnow()) - job.started_at).total_seconds())
    return int(total)


def _book_segment(db, job: "models.Job", now: datetime, reason: str) -> float:
    """Charge the stretch this job has just finished running, and return it.

    Every path that takes a job off the machine goes through here, so a job
    that runs, pauses, resumes and ends leaves one ledger row per stretch and
    the hours add up to the wall-clock time it actually held the hardware.
    """
    if not job.started_at:
        return 0.0
    seconds = max(0.0, (now - job.started_at).total_seconds())
    db.add(models.UsageRecord(
        user_id=job.user_id,
        username=job.username,
        gpu_indices=job.gpu_indices or "",
        gpu_count=job.gpu_count,
        cpu_cores=job.cpu_cores or 0.0,
        backend="job",
        started_at=job.started_at,
        ended_at=now,
        gpu_seconds=seconds * max(0, job.gpu_count),
        cpu_seconds=seconds * (job.cpu_cores or 0.0),
        end_reason=reason,
    ))
    job.runtime_seconds = (job.runtime_seconds or 0.0) + seconds
    return seconds


def _pause(db, job: "models.Job", message: str) -> None:
    """Freeze a running job without giving up its place or its progress.

    The container stays, so its memory and its open files survive; what stops
    is the clock.  The stretch it has just run is booked now rather than at the
    end, which is what keeps a paused job from being charged for the days it
    spends waiting for Monday.
    """
    _book_segment(db, job, datetime.utcnow(), "paused")
    job.status = models.JobStatus.paused
    job.started_at = None
    job.message = message
    db.add(job)
    logger.info("Job %d for %r → paused", job.id, job.username)


def _resume(db, job: "models.Job") -> bool:
    """Thaw a paused job.  A new stretch starts, so the clock starts with it."""
    from services import audit, container_manager

    if not container_manager.unpause_job_container(job.id):
        # The container is gone, so there is nothing to resume into.
        _close(db, job, models.JobStatus.failed, None,
               "The job was paused and its container has since gone away.")
        return False
    job.status = models.JobStatus.running
    job.started_at = datetime.utcnow()
    job.message = None
    db.add(job)
    audit.record(db, "job.budget_resumed", actor="system",
                 target=f"job:{job.id}", detail="budget refilled", commit=False)
    logger.info("Job %d for %r → resumed", job.id, job.username)
    return True


def _resume_paused(db) -> List[Dict[str, Any]]:
    """Put paused jobs back to work once their owner has budget again.

    Runs before dispatch, so a job that was already admitted and frozen goes
    back before anything new is started: it is ahead of the queue because it
    was ahead of the queue.
    """
    from services import quota

    paused = (
        db.query(models.Job)
        .filter(models.Job.status == models.JobStatus.paused)
        .all()
    )
    if not paused:
        return []

    from services import container_manager

    resumed, blockers = [], {}
    for job in paused:
        if container_manager.job_container_state(job.id) is None:
            _close(db, job, models.JobStatus.failed, None,
                   "The job was paused and its container has since gone away.")
            continue
        user = job.user
        if user is None or not user.is_active:
            continue
        if user.id not in blockers:
            blockers[user.id] = quota.time_blocker(quota.time_snapshot(db, user))
        if blockers[user.id]:
            continue
        if _resume(db, job):
            resumed.append({"id": job.id, "user": job.username})
    if resumed or blockers:
        db.commit()
    return resumed


def _requeue(db, job: "models.Job", message: str) -> None:
    """Return a running job to the queue, keeping what it has already used.

    The reverse of :func:`_close` in every respect but one: the time the job
    spent is still booked against its owner.  It ran, it held the cards, and a
    job that could be requeued for free would be a way to use the machine
    without ever paying for it.
    """
    _book_segment(db, job, datetime.utcnow(), "requeued")

    job.status = models.JobStatus.queued
    job.runtime_seconds = 0.0   # it will run again from the beginning
    job.container_id = None
    job.gpu_indices = None
    job.gpu_memory_used_mb = None
    job.gpu_memory_peak_mb = None
    job.exit_code = None
    job.started_at = None
    job.finished_at = None
    job.message = message
    db.add(job)
    logger.info("Job %d for %r → back in the queue", job.id, job.username)


def _check_output_size(db, job: "models.Job") -> Optional[str]:
    """Flag, or, if configured, stop, a job whose log is running away.

    Progress bars are a single line in the file, but they keep appending to it.
    The owner has no way to notice until their disk quota is gone, so the size
    is surfaced on the job itself.
    """
    from services import container_manager, safepath

    warn_mb = settings.JOB_OUTPUT_WARN_MB or 0
    hard_mb = settings.JOB_MAX_OUTPUT_MB or 0
    if not (warn_mb or hard_mb) or not job.output_path:
        return None

    megabytes = safepath.size(workspace_root(job.username), job.output_path) // 1048576

    if hard_mb and megabytes >= hard_mb:
        container_manager.stop_job_container(job.id)
        container_manager.remove_job_container(job.id)
        _close(db, job, models.JobStatus.failed, None,
               f"Stopped: its output file reached {megabytes} MB. Print progress "
               "less often, for example tqdm(..., mininterval=10).")
        return "stopped"

    if warn_mb and megabytes >= warn_mb:
        note = (f"Its output file is {megabytes} MB and still growing, and this counts "
                "against your disk quota. Print progress less often, for example "
                "tqdm(..., mininterval=10).")
        if job.message != note:
            job.message = note
            db.add(job)
            return "noted"
    return None


def _close(db, job: "models.Job", status_value, exit_code, message) -> None:
    job.status = status_value
    job.exit_code = exit_code
    job.message = message
    job.finished_at = datetime.utcnow()
    job.container_id = None
    db.add(job)
    # Book the last stretch against the owner, so jobs count toward their
    # budget and their statistics the way interactive sessions do.  A job with
    # no GPU is recorded too: it consumed CPU, and leaving it out would hide
    # real usage from the reports and from fair share.
    _book_segment(db, job, job.finished_at, status_value.value)
    logger.info("Job %d for %r → %s", job.id, job.username, status_value.value)


def _dispatch_queued(db) -> List[Dict[str, Any]]:
    """Start queued jobs that fit, fairest-first, respecting per-user limits.

    Order is by fair-share score, not submission time: ten jobs from one user
    must not push everyone else behind them.  Ties, including every job of the
    same user, fall back to submission time, so within one user the queue is
    still first-come-first-served.
    """
    from services import container_manager, fairshare, quota

    queued = (
        db.query(models.Job)
        .filter(models.Job.status == models.JobStatus.queued)
        .all()
    )
    if not queued:
        return []

    # Paused jobs count here: they are on the machine and they will want their
    # slot back, so a user with two frozen jobs is at their limit.
    running_per_user: Dict[int, int] = defaultdict(int)
    in_flight = (models.JobStatus.starting, models.JobStatus.running,
                 models.JobStatus.paused)
    for job in db.query(models.Job).filter(models.Job.status.in_(in_flight)).all():
        running_per_user[job.user_id] += 1

    availability = gpu_availability(db)
    cpu = cpu_capacity(db)
    free_cores = cpu["free_cores"]
    share = fairshare.scores(db)
    # One budget lookup per user per pass, not one per queued job: a user with
    # thirty jobs waiting would otherwise pay for thirty identical answers.
    blockers: Dict[int, Optional[str]] = {}
    started = []

    # Re-sorted after each admission, because admitting a job changes its
    # owner's score and therefore who should go next.
    remaining = list(queued)
    while remaining:
        remaining.sort(key=lambda j: (share.get(j.user_id, 0.0), j.created_at, j.id))
        job = remaining.pop(0)

        if running_per_user[job.user_id] >= settings.JOB_MAX_RUNNING_PER_USER:
            continue  # this user is at their limit; others may still start

        user = job.user
        if user is None or not user.is_active:
            _close(db, job, models.JobStatus.cancelled, None, "The owning account is inactive.")
            continue

        # Out of budget, or over the disk quota: hold the job where it is.
        # Both reasons pass on their own, one when the period refills and one
        # when the user deletes something, and a job failed for a reason that
        # expires is a job somebody has to remember to submit again.  Asked
        # before placement, so a held job neither takes a GPU it cannot use nor
        # waits for one to tell its owner why it is waiting.
        if user.id not in blockers:
            blockers[user.id] = quota.job_blocker(db, user)
        blocked = blockers[user.id]
        if blocked:
            # A job just put back in the queue already carries this reason with
            # the rest of the explanation attached; replacing it with the bare
            # sentence would drop the half that says what happens next.
            if not (job.message or "").startswith(blocked):
                job.message = blocked
                db.add(job)
                db.commit()
            continue

        job_cores = cores_for(user)
        if job_cores > free_cores:
            continue  # not enough CPU left; a smaller job may still fit

        placement = _place(job, availability)
        if placement is None:
            continue  # no GPU has room, leave it queued, try again next pass

        job.gpu_indices = ",".join(str(i) for i in placement)
        job.cpu_cores = job_cores
        job.status = models.JobStatus.starting
        job.started_at = datetime.utcnow()
        db.add(job)
        db.commit()

        try:
            handle = container_manager.start_job_container(job, user)
        except Exception as exc:  # noqa: BLE001 (one bad job must not stall the queue)
            logger.error("Could not start job %d: %s", job.id, exc)
            _close(db, job, models.JobStatus.failed, None,
                   "The job could not be started. Contact an administrator.")
            db.commit()
            continue

        job.container_id = handle["container_id"]
        job.status = models.JobStatus.running
        db.add(job)
        db.commit()

        running_per_user[job.user_id] += 1
        free_cores -= job_cores
        fairshare.charge(share, job.user_id, job.gpu_count, job_cores)
        for entry in availability:
            if entry["index"] in placement:
                entry["free_mb"] = max(0, entry["free_mb"] - job.gpu_memory_mb)
                entry["running_jobs"] += 1
        started.append({
            "id": job.id, "user": job.username,
            "gpus": job.gpu_indices or "none", "cores": job_cores,
        })

    return started


# ---------------------------------------------------------------------------
# Listing: the filters and ordering both job listings share
# ---------------------------------------------------------------------------

#: Statuses that mean the job is on the machine or waiting to be.
ACTIVE_STATUSES = _ACTIVE

#: What a caller may order a listing by, mapped to the column that means it.
#: Runtime is the stored figure rather than ``finished_at - started_at``,
#: because a job can run in several stretches and because the ordering has to
#: be the same on the page being looked at and on the ones that are not.
SORT_COLUMNS = {
    "id": models.Job.id,
    "user": models.Job.username,
    "name": models.Job.name,
    "status": models.Job.status,
    "created": models.Job.created_at,
    "started": models.Job.started_at,
    "finished": models.Job.finished_at,
    "runtime": models.Job.runtime_seconds,
}


def filter_jobs(query, status: Optional[str] = None, search: Optional[str] = None,
                with_user: bool = False):
    """Narrow a job query by status and by free text, both optional.

    *status* takes one status or several comma-separated; anything that is not
    a status is ignored rather than refused, so a stale bookmark shows
    everything instead of an error.
    """
    wanted = [
        name.strip() for name in (status or "").split(",")
        if name.strip() in models.JobStatus.__members__
    ]
    if wanted:
        query = query.filter(
            models.Job.status.in_([models.JobStatus[name] for name in wanted]))

    if search and search.strip():
        needle = f"%{search.strip()}%"
        fields = models.Job.name.ilike(needle) | models.Job.script.ilike(needle)
        if with_user:
            fields = fields | models.Job.username.ilike(needle)
        query = query.filter(fields)
    return query


def sort_jobs(query, sort: str = "id", order: str = "desc"):
    """Order a job listing, always with the id as a tie-break.

    Most of these columns have ties, an unnamed job or a shared status, and a
    sort that leaves ties unordered shuffles rows between pages under a poll.
    """
    column = SORT_COLUMNS.get(sort, models.Job.id)
    direction = column.asc() if order == "asc" else column.desc()
    return query.order_by(direction, models.Job.id.desc())


def paginate(query, page: int, per_page: int):
    """``(rows, pagination)``, clamped so a page past the end is not blank."""
    total = query.count()
    pages = max(1, -(-total // per_page))
    page = min(max(1, page), pages)
    rows = query.offset((page - 1) * per_page).limit(per_page).all()
    return rows, {"page": page, "per_page": per_page, "total": total, "pages": pages}


def status_counts(db, user_id: Optional[int] = None) -> Dict[str, int]:
    """How many jobs are in each status, over everything rather than a page."""
    from sqlalchemy import func

    query = db.query(models.Job.status, func.count(models.Job.id))
    if user_id is not None:
        query = query.filter(models.Job.user_id == user_id)
    return {status.value: count for status, count in query.group_by(models.Job.status).all()}


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def queued_blockers(db) -> Dict[int, Optional[str]]:
    """Why each queued job is not running yet, in its own words.

    This replays the dispatcher over the same state without touching anything,
    which is the only way to answer the question honestly: whether a job can
    start depends on what the jobs *ahead* of it will take, so asking about
    one job in isolation would say "there is room" right up until the moment
    the job in front takes that room.

    ``None`` means nothing is blocking it: it is next, and will start on the
    coming pass.  A string is the reason, phrased for the person waiting.
    """
    from services import fairshare, quota

    queued = (
        db.query(models.Job)
        .filter(models.Job.status == models.JobStatus.queued)
        .all()
    )
    if not queued:
        return {}

    running_per_user: Dict[int, int] = defaultdict(int)
    in_flight = (models.JobStatus.starting, models.JobStatus.running,
                 models.JobStatus.paused)
    for job in db.query(models.Job).filter(models.Job.status.in_(in_flight)).all():
        running_per_user[job.user_id] += 1

    availability = gpu_availability(db)
    free_cores = cpu_capacity(db)["free_cores"]
    share = fairshare.scores(db)

    reasons: Dict[int, Optional[str]] = {}
    blocked_by_user: Dict[int, Optional[str]] = {}
    remaining = list(queued)
    while remaining:
        # Same ordering as the dispatcher, re-sorted after each admission.
        remaining.sort(key=lambda j: (share.get(j.user_id, 0.0), j.created_at, j.id))
        job = remaining.pop(0)

        if running_per_user[job.user_id] >= settings.JOB_MAX_RUNNING_PER_USER:
            reasons[job.id] = (
                f"you already have {settings.JOB_MAX_RUNNING_PER_USER} job(s) running, "
                "this one starts when one of them ends"
            )
            continue

        user = job.user
        if user is None or not user.is_active:
            reasons[job.id] = "the owning account is inactive"
            continue

        if user.id not in blocked_by_user:
            blocked_by_user[user.id] = quota.job_blocker(db, user)
        if blocked_by_user[user.id]:
            reasons[job.id] = blocked_by_user[user.id]
            continue

        job_cores = cores_for(user)
        if job_cores > free_cores:
            reasons[job.id] = (
                f"waiting for CPU: it needs {job_cores:g} cores and "
                f"{free_cores:g} are free"
            )
            continue

        placement = _place(job, availability)
        if placement is None:
            from services import gpu_monitor

            if job.gpu_count and not gpu_monitor.telemetry()["ok"]:
                reasons[job.id] = (
                    "the platform cannot read the GPUs at the moment, so it "
                    "will not place work on them; an administrator has been "
                    "told and your job starts as soon as that is fixed"
                )
                continue
            largest = max((g["free_mb"] for g in availability), default=0)
            if job.gpu_count > 1:
                fits = sum(1 for g in availability if g["free_mb"] >= job.gpu_memory_mb)
                reasons[job.id] = (
                    f"waiting for GPUs: it needs {job.gpu_count} cards with "
                    f"{job.gpu_memory_mb} MiB free each, and {fits} have that much"
                )
            else:
                reasons[job.id] = (
                    f"waiting for GPU memory: it needs {job.gpu_memory_mb} MiB free "
                    f"and the emptiest card has {largest} MiB"
                )
            continue

        # It would start on this pass.  Charge it to the simulated state so the
        # jobs behind it are judged against what is left, not against what was
        # free before it took its share.
        reasons[job.id] = None
        running_per_user[job.user_id] += 1
        free_cores -= job_cores
        fairshare.charge(share, job.user_id, job.gpu_count, job_cores)
        for entry in availability:
            if entry["index"] in placement:
                entry["free_mb"] = max(0, entry["free_mb"] - job.gpu_memory_mb)
                entry["running_jobs"] += 1

    return reasons


def queue_view(db) -> List[Dict[str, Any]]:
    """The shared queue: every job in flight, whoever owns it.

    Someone deciding whether to submit needs to see the whole queue, not their
    own corner of it, a machine with eleven jobs waiting is a different
    decision from an idle one.  So this crosses user boundaries, and therefore
    carries only what is public: who, what it is called, what it holds, how
    long it has waited.  No paths, no output, no scripts.
    """
    rows = (
        db.query(models.Job)
        .filter(models.Job.status.in_(_ACTIVE))
        .order_by(models.Job.id.desc())
        .all()
    )
    if not rows:
        return []

    order = queued_order(db)
    now = datetime.utcnow()
    out = []
    for job in rows:
        # For a job still waiting this is how long it has waited so far; for
        # one that started, how long it waited before it did.
        paused = job.status == models.JobStatus.paused
        reference = job.started_at or now
        out.append({
            "id": job.id,
            "username": job.username,
            "name": job.name,
            "status": job.status.value,
            "gpu_count": job.gpu_count,
            "gpu_memory_mb": job.gpu_memory_mb,
            "gpus": _parse_indices(job.gpu_indices),
            "queue_position": order.get(job.id),
            "runtime_seconds": (
                _elapsed_seconds(job, now) if (job.started_at or paused) else None
            ),
            # A frozen job is not waiting for anything the queue can give it,
            # so a growing "waited" figure would be a lie.
            "waited_seconds": (
                None if paused
                else max(0, int((reference - job.created_at).total_seconds()))
            ),
        })

    # Running first, because that is what the machine is actually doing, then the
    # waiting ones in the order they will be served.
    def sort_key(row):
        if row["status"] in ("running", "starting", "paused"):
            return (0, -(row["runtime_seconds"] or 0))
        return (1, row["queue_position"] or 9999)

    out.sort(key=sort_key)
    return out


def queued_order(db) -> Dict[int, int]:
    """``{job id: 1-based position}`` in the order the scheduler will serve.

    This is fair-share order, not submission order.  Showing the arrival index
    would be actively misleading: a job submitted eleventh can be served first
    because its owner has been given the least, and telling them they are
    "11th" would look like the queue was stuck.
    """
    from services import fairshare

    queued = (
        db.query(models.Job)
        .filter(models.Job.status == models.JobStatus.queued)
        .all()
    )
    if not queued:
        return {}

    share = fairshare.scores(db)
    # Mirror the dispatcher: admitting a job raises its owner's score, so the
    # projected order interleaves users the same way the real one will.
    projected = dict(share)
    remaining = list(queued)
    order: Dict[int, int] = {}
    position = 1
    while remaining:
        remaining.sort(key=lambda j: (projected.get(j.user_id, 0.0), j.created_at, j.id))
        job = remaining.pop(0)
        order[job.id] = position
        position += 1
        fairshare.charge(projected, job.user_id, job.gpu_count, job.cpu_cores or 0.0)
    return order


def queue_position(db, job: "models.Job", order: Optional[Dict[int, int]] = None) -> Optional[int]:
    """Where this job sits in the scheduler's order (None once it has started)."""
    if job.status != models.JobStatus.queued:
        return None
    if order is None:
        order = queued_order(db)
    return order.get(job.id)


# Terminal control sequences: fine in a terminal, unreadable in a browser.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def _for_display(raw: bytes, tail_lines: int, max_line: int) -> str:
    """Turn a slice of a log file into something readable on a web page."""
    text = raw.decode("utf-8", errors="replace")
    # Split on newlines ONLY.  str.splitlines() also breaks on \r, which turns
    # a progress bar rewriting one line into hundreds of separate lines and
    # floods the line budget with frames nobody wants to read.
    lines = []
    for line in text.split("\n"):
        # A progress bar rewrites one line with \r; once this is a log rather
        # than a live terminal, only the final state carries information.
        if "\r" in line:
            line = line.split("\r")[-1]
        line = _ANSI.sub("", line)
        if len(line) > max_line:
            line = line[:max_line] + f"… ({len(line) - max_line} more characters)"
        lines.append(line)
    return "\n".join(lines[-tail_lines:])


def read_output(job: "models.Job", tail_lines: Optional[int] = None) -> Dict[str, Any]:
    """The end of a job's output file, bounded and symlink-safe.

    Never reads the whole file: a job that prints a line per training step can
    produce gigabytes, and the size on disk must not decide how much memory the
    platform uses or how much crosses the network.

    The path is resolved through :mod:`services.safepath` because the file sits
    in a directory its owner controls, replacing it with a symlink would
    otherwise have the backend, which runs as root, read whatever it points at.
    """
    empty = {"text": "", "truncated": False, "size_bytes": 0}
    if not job.output_path:
        return empty

    from services import safepath

    tail_lines = tail_lines or settings.JOB_OUTPUT_TAIL_LINES
    try:
        raw, size = safepath.read_tail(
            workspace_root(job.username), job.output_path,
            settings.JOB_OUTPUT_TAIL_BYTES,
        )
    except safepath.UnsafePath as exc:
        logger.warning("Refusing to read job %d output: %s", job.id, exc)
        return {**empty, "text": "(the output file was replaced and cannot be shown)"}
    except OSError:
        return empty

    text = _for_display(raw, tail_lines, settings.JOB_OUTPUT_MAX_LINE_CHARS)
    shown = len(raw)
    return {
        "text": text,
        # True when there is more on disk than what is shown, so the UI can
        # point at the file instead of pretending this is everything.
        "truncated": size > shown or text.count("\n") + 1 >= tail_lines,
        "size_bytes": size,
    }


def view(
    db,
    job: "models.Job",
    with_output: bool = False,
    order: Optional[Dict[int, int]] = None,
    blocked_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Serialise a job.  Pass *order* when rendering a list, computing the
    queue order once per request rather than once per row."""
    payload = {
        "id": job.id,
        "name": job.name,
        "user": job.username,
        "script": job.script,
        "workdir": job.workdir,
        "output": job.output_path,
        "gpu_count": job.gpu_count,
        "gpu_memory_mb": job.gpu_memory_mb,
        # What the job may hold before the scheduler stops it, so the figure
        # the user is held to is one they can actually see.
        "gpu_memory_allowance_mb": (
            gpu_overrun_allowance_mb(job.gpu_memory_mb) if job.gpu_memory_mb else None
        ),
        "gpu_memory_used_mb": job.gpu_memory_used_mb,
        # What it held at its highest, which is the figure worth reading once
        # the job has ended and the live one has fallen back to zero.
        "gpu_memory_peak_mb": job.gpu_memory_peak_mb,
        # Whether going over the allowance stops this job.  The warning has to
        # say which it is: telling someone their job is about to be killed
        # when it is not is its own kind of wrong.
        "gpu_memory_enforced": bool(job.gpu_memory_enforced),
        "max_runtime_minutes": job.max_runtime_minutes,
        "cpu_cores": job.cpu_cores or None,
        "gpus": _parse_indices(job.gpu_indices),
        "status": job.status.value,
        "exit_code": job.exit_code,
        "message": job.message,
        "queue_position": queue_position(db, job, order),
        # How long it waited to be picked up, still counting while it waits.
        # None once it is frozen: it is not waiting for the queue any more.
        "waited_seconds": None if job.status == models.JobStatus.paused else max(0, int(
            ((job.started_at or datetime.utcnow()) - job.created_at).total_seconds()
        )),
        "created_at": job.created_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }
    if job.status == models.JobStatus.queued:
        # Why it is not running yet.  Empty string means "nothing is stopping
        # it", meaning it is next in line, which is not the same as not having asked.
        payload["blocked_reason"] = blocked_reason
    if job.started_at or job.runtime_seconds:
        payload["runtime_seconds"] = _elapsed_seconds(job, job.finished_at)
    if with_output:
        tail = read_output(job)
        payload["output_tail"] = tail["text"]
        payload["output_truncated"] = tail["truncated"]
        payload["output_size_bytes"] = tail["size_bytes"]
        payload["output_tail_lines"] = settings.JOB_OUTPUT_TAIL_LINES
    return payload
