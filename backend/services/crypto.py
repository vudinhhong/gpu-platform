"""Symmetric encryption for secrets that must be shown back to their owner.

Jupyter session tokens and per-session SSH passwords cannot be hashed, the
dashboard has to display them again, but storing them as plaintext meant a
single leaked ``gpu_platform.db`` handed over every live session.

The key is derived from ``SECRET_KEY`` (already required to be high-entropy)
so there is no second secret to distribute.  Rotating ``SECRET_KEY``
invalidates stored ciphertexts; :func:`decrypt` degrades to ``None`` rather
than raising, and the affected value is regenerated on the next session start.
"""

import base64
import hashlib
import logging
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)

_PREFIX = "enc:v1:"


def _fernet():
    from cryptography.fernet import Fernet

    digest = hashlib.sha256(f"gpu-platform-secretbox::{settings.SECRET_KEY}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(plaintext: Optional[str]) -> Optional[str]:
    """Encrypt *plaintext*; returns a prefixed, storable string."""
    if not plaintext:
        return None
    try:
        return _PREFIX + _fernet().encrypt(plaintext.encode()).decode()
    except Exception as exc:  # noqa: BLE001 (never lose a session over this)
        logger.error("Secret encryption failed, storing nothing: %s", exc)
        return None


def decrypt(stored: Optional[str]) -> Optional[str]:
    """Decrypt a value produced by :func:`encrypt`.

    Values written before encryption existed (no prefix) are returned as-is so
    upgrading the platform does not invalidate running sessions.
    """
    if not stored:
        return None
    if not stored.startswith(_PREFIX):
        return stored  # legacy plaintext row
    try:
        return _fernet().decrypt(stored[len(_PREFIX):].encode()).decode()
    except Exception:  # noqa: BLE001 (rotated SECRET_KEY or corrupt value)
        return None
