"""Authentication endpoints: login, logout, current-user info, password change."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

import models
from auth import (
    apply_derived_credentials,
    create_access_token,
    get_current_user,
    get_password_hash,
    revoke_tokens,
    validate_password,
    verify_password,
)
from config import settings
from database import get_db
from routers.proxy import PROXY_COOKIE
from schemas import MessageResponse, SelfPasswordChange, UserResponse
from services import audit, ratelimit

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _sync_container_password(user: models.User) -> bool:
    """Update the UNIX password inside the user's running container.

    Returns True when a live container took the new password, which is what
    makes "your SSH password changed too" a statement about now rather than
    about the next session start.
    """
    from services import container_manager

    if not settings.UNIFIED_PASSWORD:
        return False
    applied = container_manager.update_container_password(
        user.username, user.unix_password_hash
    )
    # Jupyter reads its config at startup, so this applies from the next
    # session start; the dashboard link keeps working in the meantime because
    # the proxy authenticates with the session token, not the password.
    if not user.hashed_jupyter_password:
        container_manager.write_jupyter_auth_config(
            user.username, user.account_jupyter_hash, password_required=False
        )
    return applied


def _cookie_secure(request: Request) -> bool:
    """Set the Secure flag whenever the browser is actually talking HTTPS.

    The reverse proxy terminates TLS, so the scheme has to come from
    X-Forwarded-Proto; COOKIE_SECURE forces it on for deployments that always
    run behind TLS.
    """
    if settings.COOKIE_SECURE:
        return True
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


def _set_proxy_cookie(response: Response, request: Request, token: str) -> None:
    """Hand the browser a JWT it can present when navigating to /jupyter/*.

    Plain navigation cannot add an Authorization header, which is why the
    proxy used to wave through every password-mode request.  An HttpOnly
    cookie scoped to /jupyter closes that hole without a token in the URL.
    """
    response.set_cookie(
        key=PROXY_COOKIE,
        value=token,
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(request),
        path="/jupyter",
    )


@router.post("/login", response_model=dict)
async def login(
    request: Request,
    response: Response,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    """OAuth2 password flow, returns a JWT plus a snapshot of the user record."""
    ip = audit.client_ip(request)
    # Throttle per (username, IP): neither a password spray across accounts nor
    # a focused attack on one account gets unlimited attempts.
    keys = [f"user:{form_data.username}", f"ip:{ip or 'unknown'}"]
    for key in keys:
        allowed, retry_after = ratelimit.check(key)
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Too many failed attempts. Try again in {retry_after}s.",
                headers={"Retry-After": str(retry_after)},
            )

    user = db.query(models.User).filter(models.User.username == form_data.username).first()
    if user is None or not verify_password(form_data.password, user.hashed_password):
        for key in keys:
            ratelimit.record_failure(key)
        audit.record(
            db, "auth.login_failed", actor=form_data.username, ip_address=ip,
            detail="bad credentials",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if user.deleted_at is not None:
        audit.record(
            db, "auth.login_denied", actor=user.username, ip_address=ip,
            detail="account removed",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account has been removed. Contact an administrator.",
        )
    if not user.is_active:
        audit.record(
            db, "auth.login_denied", actor=user.username, ip_address=ip,
            detail="account deactivated",
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is deactivated. Contact an administrator.",
        )

    for key in keys:
        ratelimit.record_success(key)

    # Backfill the Jupyter/UNIX credentials derived from this password for
    # accounts created before unified passwords existed.  Login is the only
    # other moment the plaintext is legitimately in hand.
    if settings.UNIFIED_PASSWORD and not (user.account_jupyter_hash and user.unix_password_hash):
        apply_derived_credentials(user, form_data.password)
        db.add(user)
        db.commit()

    token = create_access_token(user)
    _set_proxy_cookie(response, request, token)
    audit.record(db, "auth.login", actor=user.username, ip_address=ip)
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": UserResponse.model_validate(user).model_dump(mode="json"),
    }


@router.post("/logout", response_model=MessageResponse)
async def logout(
    response: Response,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Discard the proxy cookie; the client drops its own copy of the JWT."""
    response.delete_cookie(PROXY_COOKIE, path="/jupyter")
    audit.record(db, "auth.logout", actor=current_user.username)
    return {"message": f"User {current_user.username} logged out successfully."}


@router.get("/me", response_model=UserResponse)
async def read_current_user(current_user: models.User = Depends(get_current_user)):
    """Return the authenticated user's profile."""
    return current_user


@router.put("/password", response_model=dict)
async def change_password(
    payload: SelfPasswordChange,
    request: Request,
    response: Response,
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Let the logged-in user change their own password, everywhere at once.

    One password covers the platform, SSH and Jupyter, so all three move
    together: the UNIX password inside a running container is replaced on the
    spot (sshd consults it per authentication, so the change is immediate) and
    Jupyter's config file is rewritten for its next start.

    Every token issued before the change is revoked, so a session stolen
    earlier cannot outlive it, but the browser doing the changing is handed a
    fresh one here.  Making the user sign in again bought nothing: they had
    just proved they know both passwords, and being thrown out mid-run is how
    a security measure teaches people to avoid changing their password.  The
    Jupyter session keeps running untouched either way; the proxy
    authenticates with the session token, not with this password.
    """
    if not verify_password(payload.old_password, current_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect",
        )
    validate_password(payload.new_password)
    if verify_password(payload.new_password, current_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The new password must be different from the current one.",
        )

    current_user.hashed_password = get_password_hash(payload.new_password)
    apply_derived_credentials(current_user, payload.new_password)

    # A user who set a SEPARATE Jupyter password chose a second factor on
    # purpose, so it is not silently overwritten: the form asks, and only an
    # explicit yes puts Jupyter back on the account password.
    jupyter_reset = bool(
        current_user.hashed_jupyter_password and payload.reset_jupyter_password
    )
    if jupyter_reset:
        current_user.hashed_jupyter_password = None

    revoke_tokens(current_user)
    db.add(current_user)
    # Push the new password into the running container so SSH keeps working
    # without a session restart.
    ssh_applied = _sync_container_password(current_user)

    # A session started before unified passwords carries its own generated SSH
    # password, and the dashboard still shows it.  The container just stopped
    # accepting it, so it must not keep being displayed as the way in.
    session = current_user.jupyter_session
    if ssh_applied and session is not None and session.ssh_password:
        session.ssh_password = None
        db.add(session)

    # A job token is a second credential: it lives in a file, not in the
    # password, so revoking every JWT above would leave it working.  Rotate it
    # here, the commands read the file on each run, so a workspace that is
    # open keeps working, while any copy taken before the change stops.
    if settings.JOBS_ENABLED:
        from services import jobs as job_service

        job_service.ensure_job_token(db, current_user)
    audit.record(
        db, "auth.password_changed", actor=current_user.username,
        detail="jupyter_password_reset" if jupyter_reset else None,
        ip_address=audit.client_ip(request), commit=False,
    )
    db.commit()
    db.refresh(current_user)

    # Minted after the commit so it carries the bumped token version.
    token = create_access_token(current_user)
    _set_proxy_cookie(response, request, token)

    notes = []
    if ssh_applied:
        # sshd checks the password on every authentication, Jupyter reads its
        # config once at startup, so the two land at different moments and
        # saying "changed everywhere" would be a quarter true.
        notes.append(
            "SSH accepts it right away; Jupyter picks it up the next time you "
            "start your workspace. The one you have open keeps running."
        )
    elif settings.UNIFIED_PASSWORD:
        notes.append("SSH and Jupyter use it from your next session start.")
    if jupyter_reset:
        notes.append("Your separate Jupyter password was removed.")

    return {
        "message": " ".join(["Password updated.", *notes]),
        "access_token": token,
        "token_type": "bearer",
        "ssh_updated": ssh_applied,
        "jupyter_password_reset": jupyter_reset,
        "user": UserResponse.model_validate(current_user).model_dump(mode="json"),
    }
