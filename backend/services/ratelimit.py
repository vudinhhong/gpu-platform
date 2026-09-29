"""In-process rate limiting and account lockout for the login endpoint.

Without this, ``POST /api/auth/login`` accepted unlimited guesses, a six
character minimum password plus an unauthenticated endpoint is a brute-force
invitation.

Scope: one backend process (the platform runs a single uvicorn worker by
design, see backend/Dockerfile).  With multiple workers or replicas this
should move to Redis; the interface below is deliberately small enough to
swap.
"""

import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Optional, Tuple

from config import settings

_lock = threading.Lock()
# key → timestamps of recent failures
_failures: Dict[str, Deque[float]] = defaultdict(deque)
# key → unix time until which the key is locked out
_locked_until: Dict[str, float] = {}


def _prune(key: str, now: float) -> None:
    window = settings.LOGIN_FAIL_WINDOW_SECONDS
    bucket = _failures[key]
    while bucket and now - bucket[0] > window:
        bucket.popleft()


def check(key: str) -> Tuple[bool, int]:
    """``(allowed, retry_after_seconds)`` for this key."""
    now = time.time()
    with _lock:
        until = _locked_until.get(key, 0.0)
        if until > now:
            return False, int(until - now) + 1
        if until:
            del _locked_until[key]
        return True, 0


def record_failure(key: str) -> Optional[int]:
    """Record a failed attempt; returns lockout seconds when it trips."""
    now = time.time()
    with _lock:
        _prune(key, now)
        _failures[key].append(now)
        if len(_failures[key]) >= settings.LOGIN_MAX_FAILURES:
            _failures[key].clear()
            _locked_until[key] = now + settings.LOGIN_LOCKOUT_SECONDS
            return settings.LOGIN_LOCKOUT_SECONDS
    return None


def record_success(key: str) -> None:
    with _lock:
        _failures.pop(key, None)
        _locked_until.pop(key, None)


def status() -> Dict[str, int]:
    """Currently locked keys → seconds remaining (admin visibility)."""
    now = time.time()
    with _lock:
        return {
            key: int(until - now)
            for key, until in _locked_until.items()
            if until > now
        }
