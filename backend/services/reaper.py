"""Idle-session reaper and the budget passes that run beside it.

``JupyterSession.last_activity`` existed from day one but nothing ever wrote
to it, so sessions people forgot about held their GPUs forever.  The proxy now
stamps it on real traffic (routers.proxy.touch_activity) and this loop returns
the hardware.

Disabled unless ``IDLE_TIMEOUT_MINUTES`` is set, reaping is a policy choice,
not a default the operator should discover by losing work.
"""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List

import models
from config import settings

logger = logging.getLogger(__name__)


def enforce_disk_quota() -> List[Dict[str, Any]]:
    """Act on users who are over their disk quota *while running*.

    The quota is checked when a session starts, but nothing stops a running
    notebook from filling the disk afterwards, and no filesystem-level quota is
    in play, the data directory is a plain bind mount, which is why `df`
    inside a container shows the whole host disk.  This pass is the enforcement.
    """
    from database import SessionLocal
    from services import audit, quota, session_backend, usage

    action = (settings.DISK_QUOTA_ACTION or "warn").lower()
    offenders: List[Dict[str, Any]] = []
    db = SessionLocal()
    try:
        running = (
            db.query(models.JupyterSession)
            .filter(models.JupyterSession.status == models.SessionStatus.running)
            .all()
        )
        for session in running:
            user = session.user
            limit = quota.disk_quota_mb(user)
            if not limit:
                continue
            # Measured where the workspace actually is: the cached scan only
            # covers JUPYTER_DATA_DIR, so a user mapped to a home directory
            # read as 0 MB and was never enforced no matter how full it got.
            used = quota.disk_used_mb(user.username, user)
            if used <= limit:
                continue

            offenders.append({"user": user.username, "used_mb": used, "quota_mb": limit})
            detail = f"{used} MB used of {limit} MB quota"

            # A session that has only just started is the user's chance to
            # clean up: stopping it immediately would put them straight back
            # where they were, with no way to get under the limit.
            grace = max(0, int(settings.DISK_QUOTA_GRACE_MINUTES or 0))
            age_minutes = (
                datetime.utcnow() - (session.created_at or datetime.utcnow())
            ).total_seconds() / 60.0
            within_grace = grace and age_minutes < grace

            if action == "stop" and within_grace:
                logger.info(
                    "%r is over disk quota (%s) but started %.0f min ago, "
                    "leaving it running to clean up", user.username, detail, age_minutes,
                )
            elif action == "stop":
                try:
                    session_backend.stop_session(
                        user.username, session.container_id
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error("Quota stop failed for %r: %s", user.username, exc)
                    continue
                session.status = models.SessionStatus.stopped
                session.pid = None
                session.container_id = None
                db.add(session)
                usage.close_open_records(db, user, reason="quota", commit=False)
                audit.record(db, "quota.session_stopped", actor="system",
                             target=user.username, detail=detail, commit=False)
                logger.warning("Stopped %r for exceeding disk quota: %s", user.username, detail)
            else:
                audit.record(db, "quota.exceeded", actor="system",
                             target=user.username, detail=detail, commit=False)
                logger.warning("%r is over disk quota: %s", user.username, detail)

        if offenders:
            db.commit()
    except Exception as exc:  # noqa: BLE001 (never kill the loop)
        logger.error("Disk quota enforcement failed: %s", exc)
        db.rollback()
    finally:
        db.close()
    return offenders


def publish_time_budgets() -> List[Dict[str, Any]]:
    """Refresh each running workspace's view of its owner's job budget.

    Nothing is enforced here, and that is the point: the GPU-hour and CPU-hour
    budgets are charged to the queue, so they are applied where jobs are
    admitted and while they run (``services.jobs``), never against somebody's
    interactive session.  A session that has gone idle is the idle reaper's
    business, not the budget's.

    What this pass does is write the figures into the user's workspace, because
    they move every minute a job of theirs is running and ``limits`` inside the
    container has no other way to know them.  The list it returns is whoever
    has run out, which is worth a line in the log for an administrator watching
    the queue.
    """
    from database import SessionLocal
    from services import container_manager, quota

    spent: List[Dict[str, Any]] = []
    db = SessionLocal()
    try:
        running = (
            db.query(models.JupyterSession)
            .filter(models.JupyterSession.status == models.SessionStatus.running)
            .all()
        )
        for session in running:
            user = session.user
            if user is None:
                continue

            state = quota.time_snapshot(db, user)
            container_manager.write_budget_file(user.username, state)

            if quota.time_blocker(state):
                spent.append({
                    "user": user.username,
                    "gpu_hours": state["gpu_hours"]["used"],
                    "cpu_hours": state["cpu_hours"]["used"],
                })
        if spent:
            logger.info("Out of job budget this period: %s", spent)
    except Exception as exc:  # noqa: BLE001 (never kill the loop)
        logger.error("Budget refresh failed: %s", exc)
    finally:
        db.close()
    return spent


def reap_idle() -> List[Dict[str, Any]]:
    """Stop every session idle beyond the configured timeout."""
    timeout = int(settings.IDLE_TIMEOUT_MINUTES or 0)
    if timeout <= 0:
        return []

    from database import SessionLocal
    from services import audit, session_backend, usage

    cutoff = datetime.utcnow() - timedelta(minutes=timeout)
    stopped: List[Dict[str, Any]] = []
    db = SessionLocal()
    try:
        candidates = (
            db.query(models.JupyterSession)
            .filter(
                models.JupyterSession.status == models.SessionStatus.running,
                models.JupyterSession.last_activity < cutoff,
            )
            .all()
        )
        for session in candidates:
            username = session.user.username
            idle_minutes = int(
                (datetime.utcnow() - session.last_activity).total_seconds() // 60
            )
            try:
                session_backend.stop_session(username, session.container_id)
            except Exception as exc:  # noqa: BLE001 (keep reaping the rest)
                logger.error("Idle reap failed for %r: %s", username, exc)
                continue

            session.status = models.SessionStatus.stopped
            session.pid = None
            session.container_id = None
            db.add(session)
            usage.close_open_records(db, session.user, reason="idle", commit=False)
            audit.record(
                db, "session.reaped", actor="system", target=username,
                detail=f"idle for {idle_minutes} min (timeout {timeout} min)",
                commit=False,
            )
            stopped.append({"user": username, "idle_minutes": idle_minutes})

        if stopped:
            db.commit()
            logger.info("Idle reaper stopped %d session(s): %s", len(stopped), stopped)
    except Exception as exc:  # noqa: BLE001 (never kill the loop)
        logger.error("Idle reaper pass failed: %s", exc)
        db.rollback()
    finally:
        db.close()
    return stopped
