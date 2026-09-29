"""Authentication utilities: password hashing, JWT creation/verification,
and FastAPI dependency injectors for route protection."""

from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

import models
from config import settings
from database import get_db

ALGORITHM = "HS256"

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ---------------------------------------------------------------------------
# Password hashing & policy
# ---------------------------------------------------------------------------

def verify_password(plain: str, hashed: str) -> bool:
    """Return True if *plain* matches the *hashed* bcrypt digest."""
    return _pwd_context.verify(plain, hashed)


def get_password_hash(password: str) -> str:
    """Return a bcrypt hash of *password*."""
    return _pwd_context.hash(password)


def validate_password(password: str) -> None:
    """Raise HTTP 400 unless *password* meets the platform policy.

    Deliberately modest but no longer trivial: the old six-character floor on
    an unthrottled login endpoint was brute-forceable in minutes.
    """
    problems = []
    if len(password or "") < settings.MIN_PASSWORD_LENGTH:
        problems.append(f"at least {settings.MIN_PASSWORD_LENGTH} characters")
    if not any(c.isalpha() for c in password or ""):
        problems.append("at least one letter")
    if not any(c.isdigit() for c in password or ""):
        problems.append("at least one digit")
    if problems:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Password must contain " + ", ".join(problems) + ".",
        )


# Jupyter tags its argon2 hashes with this prefix and dispatches on it.
# jupyter_server.auth.security.passwd_check() treats anything WITHOUT it as a
# legacy "algorithm:salt:digest" string, fails to split it, and returns False,
# so a correct password was rejected as invalid.  The prefix is not decoration.
JUPYTER_ARGON2_PREFIX = "argon2:"


def hash_jupyter_password(plaintext: str) -> str:
    """Hash a Jupyter login password in the exact format Jupyter expects.

    Same output as ``jupyter server password`` / ``jupyter_server.auth.security
    .passwd()``: ``argon2:$argon2id$v=19$...``.  The value goes straight into
    ``IdentityProvider.hashed_password``.
    """
    from argon2 import PasswordHasher
    from argon2.exceptions import Argon2Error

    try:
        digest = PasswordHasher().hash(plaintext)
    except Argon2Error as exc:  # pragma: no cover, defensive
        raise RuntimeError(f"Could not hash Jupyter password: {exc}") from exc
    return normalize_jupyter_hash(digest)


def derive_unix_password_hash(plaintext: str) -> str:
    """sha512-crypt digest in /etc/shadow format, for ``chpasswd -e``.

    Lets a user's container accept their *account* password over SSH without
    the platform ever storing or transmitting the plaintext: the container is
    handed this digest, exactly what /etc/shadow would hold anyway.
    """
    from passlib.hash import sha512_crypt

    return sha512_crypt.hash(plaintext)


def derive_credentials(plaintext: str) -> dict:
    """All password-derived credentials for one account.

    Called wherever the plaintext is legitimately available, account creation,
    self-service change, admin reset, and successful login (which backfills
    accounts that predate this feature).
    """
    return {
        "account_jupyter_hash": hash_jupyter_password(plaintext),
        "unix_password_hash": derive_unix_password_hash(plaintext),
    }


def apply_derived_credentials(user: "models.User", plaintext: str) -> None:
    """Refresh a user's derived credentials in place (caller commits)."""
    for field, value in derive_credentials(plaintext).items():
        setattr(user, field, value)


def normalize_jupyter_hash(digest: str | None) -> str | None:
    """Ensure a stored Jupyter password hash carries the ``argon2:`` prefix."""
    if not digest:
        return digest
    if digest.startswith(JUPYTER_ARGON2_PREFIX):
        return digest
    if digest.startswith("$argon2"):
        return JUPYTER_ARGON2_PREFIX + digest
    return digest  # some other supported format, leave it alone


# ---------------------------------------------------------------------------
# JWT token helpers
# ---------------------------------------------------------------------------

def create_access_token(
    user: "models.User",
    expires_delta: Optional[timedelta] = None,
) -> str:
    """Encode a signed access token for *user*.

    The token embeds ``ver`` (the user's ``token_version``).  Bumping that
    column revokes every token already issued, the platform previously had no
    way at all to log a compromised or deactivated account out before the
    token expired on its own.
    """
    expire = datetime.utcnow() + (
        expires_delta
        if expires_delta is not None
        else timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    payload = {
        "sub": user.username,
        "ver": int(user.token_version or 0),
        "adm": bool(user.is_admin),
        "exp": expire,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)


def revoke_tokens(user: "models.User") -> None:
    """Invalidate every JWT previously issued to *user*."""
    user.token_version = int(user.token_version or 0) + 1


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------

def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> models.User:
    """Decode the bearer token and return the authenticated user.

    Raises HTTP 401 for any token problem (including a revoked token version);
    HTTP 403 for inactive accounts.
    """
    credentials_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not token:
        raise credentials_exc
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
        username: Optional[str] = payload.get("sub")
        if username is None:
            raise credentials_exc
    except JWTError:
        raise credentials_exc

    user = db.query(models.User).filter(models.User.username == username).first()
    if user is None:
        raise credentials_exc

    # Revocation check, a token minted before the last revocation is dead.
    if int(payload.get("ver", -1)) != int(user.token_version or 0):
        raise credentials_exc

    if user.deleted_at is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account has been removed. Contact an administrator.",
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is deactivated. Contact an administrator.",
        )
    return user


def require_admin(
    current_user: models.User = Depends(get_current_user),
) -> models.User:
    """Require the current user to be an admin (HTTP 403 otherwise)."""
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator privileges required.",
        )
    return current_user
