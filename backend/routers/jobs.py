"""Batch job endpoints.

Reachable two ways, because two very different callers need them:

* the dashboard, with the platform JWT;
* the ``submit`` / ``queue`` / ``cancel`` commands inside a user's workspace,
  with the job token the platform wrote there.  Those commands run on the
  user's own machine-as-they-see-it, so asking them to paste a password would
  be absurd.

Either way the caller is resolved to one account and can only ever see or touch
that account's jobs.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

import models
from auth import get_current_user, oauth2_scheme
from config import settings
from database import get_db
from services import jobs as job_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def job_caller(
    token: Optional[str] = Depends(oauth2_scheme),
    x_job_token: Optional[str] = Header(None, alias="X-Job-Token"),
    db: Session = Depends(get_db),
) -> models.User:
    """Resolve the caller from either credential."""
    if x_job_token:
        user = job_service.user_for_token(db, x_job_token)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="This workspace's job credentials are no longer valid. "
                       "Restart your workspace to refresh them.",
            )
        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your account is deactivated.",
            )
        return user
    return get_current_user(token=token, db=db)


def _owned(db: Session, user: models.User, job_id: int) -> models.Job:
    job = db.query(models.Job).filter(models.Job.id == job_id).first()
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No job {job_id}.",
        )
    if job.user_id != user.id and not user.is_admin:
        # Saying so plainly gives nothing away: the shared queue already lists
        # every active job's id and owner.  Pretending it does not exist would
        # only send the person hunting for a typo they did not make.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Job {job_id} belongs to {job.username!r}. "
                   "You can only look inside your own jobs.",
        )
    return job


@router.post("", status_code=status.HTTP_201_CREATED)
def submit_job(
    request: Request,
    payload: Dict[str, Any] = Body(...),
    user: models.User = Depends(job_caller),
    db: Session = Depends(get_db),
):
    """Queue a script to run on a GPU when one has room."""
    job = job_service.submit(
        db, user,
        script=str(payload.get("script") or ""),
        workdir=str(payload.get("workdir") or ""),
        name=payload.get("name"),
        # Left as sent: the service parses and bounds it, and a hand-written
        # request must get the same 400 as a bad flag rather than a 500 here.
        # Absent means "not mentioned", which is not the same as 0 (--no-gpu),
        # so no default is filled in here.
        gpu_count=payload.get("gpu_count"),
        gpu_memory_mb=payload.get("gpu_memory_mb"),
        max_runtime_minutes=payload.get("max_runtime_minutes"),
    )
    return job_service.view(db, job)


@router.get("")
def list_jobs(
    active_only: bool = Query(False),
    finished_only: bool = Query(False),
    status: Optional[str] = Query(None, description="one status, or several comma-separated"),
    q: Optional[str] = Query(None, max_length=128, description="match the name or the script"),
    sort: str = Query("id"),
    order: str = Query("desc"),
    limit: int = Query(100, ge=1, le=500),
    page: int = Query(1, ge=1),
    per_page: Optional[int] = Query(None, ge=1, le=200),
    user: models.User = Depends(job_caller),
    db: Session = Depends(get_db),
):
    """The caller's own jobs, newest first, plus what the GPUs look like.

    ``per_page`` turns the list into pages, which is what the dashboard asks
    for: a user with months of history should not be sent every job on every
    poll to render ten rows.  Left out, the response is the whole ``limit``
    window as before, the ``queue`` command in a workspace prints one list
    and has no pages to turn.

    ``total`` counts the jobs matching the filter, not the page, so the UI can
    say how many pages there are without a second request.
    """
    query = db.query(models.Job).filter(models.Job.user_id == user.id)
    # The dashboard asks for the two halves separately: what is running now is
    # short, changes constantly and must never be split across pages, while
    # the history is long, static and only worth fetching a page at a time.
    active_states = (
        models.JobStatus.queued, models.JobStatus.starting,
        models.JobStatus.running, models.JobStatus.paused,
    )
    if active_only:
        query = query.filter(models.Job.status.in_(active_states))
    elif finished_only:
        query = query.filter(models.Job.status.notin_(active_states))

    # History gets long, so it can be narrowed.  Both filters are applied
    # before the count, so the pager reflects what is being shown rather than
    # what exists.
    query = job_service.filter_jobs(query, status=status, search=q)
    total = query.count()
    query = job_service.sort_jobs(query, sort, order)
    if per_page:
        pages = max(1, -(-total // per_page))
        # A page that no longer exists, the last job on it was cancelled and
        # swept up, say, returns the last real one rather than nothing, so
        # the table never goes blank under a poll.
        page = min(page, pages)
        rows = query.offset((page - 1) * per_page).limit(per_page).all()
        pagination = {
            "page": page, "per_page": per_page, "total": total, "pages": pages,
        }
    else:
        rows = query.limit(limit).all()
        # One window, so one page, `total` still says how much was left out.
        pagination = {"page": 1, "per_page": limit, "total": total, "pages": 1}

    # Counted over everything the caller owns, not over the page: "2 active"
    # must not become "0 active" because those two are on page three.
    active_total = (
        db.query(models.Job)
        .filter(models.Job.user_id == user.id, models.Job.status.in_(active_states))
        .count()
    )

    order = job_service.queued_order(db)
    return {
        "jobs": [job_service.view(db, job, order=order) for job in rows],
        "pagination": pagination,
        "active_total": active_total,
        "gpus": job_service.gpu_availability(db),
        "cpu": job_service.cpu_capacity(db),
        "limits": {
            "max_running": settings.JOB_MAX_RUNNING_PER_USER,
            "max_queued": settings.JOB_MAX_QUEUED_PER_USER,
            # A GPU job must name its VRAM; there is no default to report.
            "gpu_memory_required": True,
            "max_runtime_minutes": settings.JOB_MAX_RUNTIME_MINUTES or None,
        },
    }


@router.get("/queue")
def shared_queue(
    user: models.User = Depends(job_caller),
    db: Session = Depends(get_db),
):
    """Everything in flight, whoever owns it, the queue as a whole.

    Deciding whether to submit means knowing what is ahead of you, and your
    own jobs are the one part of that which does not matter.  Public fields
    only: no scripts, no paths, no output.
    """
    return {
        # Who is asking, so the client can pick their own rows out of a list
        # that deliberately belongs to everybody.  Guessing it from a shell
        # variable would be wrong in exactly the containers that rename it.
        "you": user.username,
        "queue": job_service.queue_view(db),
        "gpus": job_service.gpu_availability(db),
        "cpu": job_service.cpu_capacity(db),
        "limits": {
            "max_running": settings.JOB_MAX_RUNNING_PER_USER,
            "max_queued": settings.JOB_MAX_QUEUED_PER_USER,
        },
    }


@router.get("/{job_id}")
def get_job(
    job_id: int,
    output: bool = Query(True),
    user: models.User = Depends(job_caller),
    db: Session = Depends(get_db),
):
    job = _owned(db, user, job_id)
    # Only a queued job has a reason to give, and working it out replays the
    # whole dispatcher, so it is not computed for jobs that are past caring.
    blocked = None
    if job.status == models.JobStatus.queued:
        blocked = job_service.queued_blockers(db).get(job.id)
    return job_service.view(db, job, with_output=output, blocked_reason=blocked)


@router.post("/cancel")
def cancel_jobs(
    payload: Dict[str, Any] = Body(...),
    user: models.User = Depends(job_caller),
    db: Session = Depends(get_db),
):
    """Cancel one or several jobs by id."""
    ids = payload.get("ids") or []
    if isinstance(ids, (int, str)):
        ids = [ids]
    try:
        ids = [int(i) for i in ids]
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Job ids must be numbers.",
        )
    if not ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Give at least one job id.",
        )
    return job_service.cancel(db, user, ids)


@router.delete("/{job_id}")
def cancel_job(
    job_id: int,
    user: models.User = Depends(job_caller),
    db: Session = Depends(get_db),
):
    _owned(db, user, job_id)
    return job_service.cancel(db, user, [job_id])
