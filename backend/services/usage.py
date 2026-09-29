"""GPU-time accounting.

One :class:`models.UsageRecord` is opened when a session starts and closed
when it stops, giving the platform a durable ledger: who held which GPUs, for
how long, on which image, and why the session ended.  Everything the quota
checker and the admin usage report need comes from this table, the live
container is gone long before the question gets asked.
"""

import logging
from datetime import datetime
from typing import List, Optional

import models

logger = logging.getLogger(__name__)


def open_record(
    db,
    user: "models.User",
    gpu_indices: str,
    image: Optional[str],
    backend: str,
    cpu_cores: float = 0.0,
) -> "models.UsageRecord":
    """Start a ledger entry, closing any stale one left by a hard crash."""
    close_open_records(db, user, reason="superseded", commit=False)

    indices = [i for i in (gpu_indices or "").split(",") if i.strip()]
    record = models.UsageRecord(
        user_id=user.id,
        username=user.username,
        gpu_indices=gpu_indices or "",
        gpu_count=len(indices),
        cpu_cores=cpu_cores or 0.0,
        image=image,
        backend=backend,
        started_at=datetime.utcnow(),
    )
    db.add(record)
    return record


def open_records(db, user: "models.User") -> List["models.UsageRecord"]:
    return (
        db.query(models.UsageRecord)
        .filter(
            models.UsageRecord.user_id == user.id,
            models.UsageRecord.ended_at.is_(None),
        )
        .all()
    )


def close_open_records(
    db, user: "models.User", reason: str, peak_memory_mb: Optional[int] = None,
    commit: bool = True,
) -> float:
    """Close every open record for *user*; returns the GPU-hours booked."""
    now = datetime.utcnow()
    booked = 0.0
    for record in open_records(db, user):
        seconds = max(0.0, (now - record.started_at).total_seconds())
        record.ended_at = now
        record.gpu_seconds = seconds * max(0, record.gpu_count)
        record.cpu_seconds = seconds * (record.cpu_cores or 0.0)
        record.end_reason = reason
        if peak_memory_mb:
            record.peak_memory_mb = peak_memory_mb
        booked += record.gpu_seconds / 3600.0
        db.add(record)
    if commit:
        db.commit()
    return booked


def note_peak_memory(db, user_id: int, used_mb: int) -> None:
    """Track the high-water RAM mark of the user's live session."""
    record = (
        db.query(models.UsageRecord)
        .filter(
            models.UsageRecord.user_id == user_id,
            models.UsageRecord.ended_at.is_(None),
        )
        .order_by(models.UsageRecord.started_at.desc())
        .first()
    )
    if record is None:
        return
    if used_mb and (record.peak_memory_mb or 0) < used_mb:
        record.peak_memory_mb = used_mb
        db.add(record)
        db.commit()
