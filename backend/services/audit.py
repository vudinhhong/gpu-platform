"""Append-only audit trail.

Every privileged or security-relevant action goes through :func:`record` so an
incident ("who deleted that account?", "when did this user get GPU 1?") can be
reconstructed after the fact.  Writes are best-effort: auditing must never be
the reason a legitimate request fails.
"""

import logging
from typing import Optional

import models

logger = logging.getLogger(__name__)


def record(
    db,
    action: str,
    actor: Optional[str] = None,
    target: Optional[str] = None,
    detail: Optional[str] = None,
    ip_address: Optional[str] = None,
    commit: bool = True,
) -> None:
    """Write one audit row.  ``commit=False`` joins the caller's transaction."""
    try:
        db.add(models.AuditLog(
            actor=actor or "system",
            action=action,
            target=target,
            detail=detail,
            ip_address=ip_address,
        ))
        if commit:
            db.commit()
    except Exception as exc:  # noqa: BLE001 (auditing never breaks the request)
        logger.error("Audit write failed (%s): %s", action, exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def client_ip(request) -> Optional[str]:
    """Caller IP, honouring the reverse proxy's X-Forwarded-For."""
    if request is None:
        return None
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None
