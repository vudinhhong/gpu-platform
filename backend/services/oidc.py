"""Sign-in through an OpenID Connect provider (Google by default).

Authorization code flow with PKCE, server side.  /api/auth/oidc/login sends
the browser to the provider with a state, a nonce and a code challenge kept in
a short-lived signed cookie; /api/auth/oidc/callback checks the state, swaps
the code for tokens at the provider's token endpoint and reads the ID token.

The ID token comes straight from the token endpoint over TLS, in exchange for
the client secret, so its signature is not checked (OpenID Connect Core
3.1.3.7, item 6); issuer, audience, expiry and nonce are.

Nothing is created here: the provider's verified email has to be the email of
an existing, active account.  The browser then gets a one-minute hand-off
token in the URL fragment and trades it for an ordinary session token, so the
session token itself never appears in a URL.
"""

import base64
import hashlib
import secrets
import time
from typing import Any, Dict, Optional

import httpx
from jose import JWTError, jwt

from config import settings

STATE_COOKIE = "oidc_state"
STATE_TTL = 600
HANDOFF_TTL = 60
_ALG = "HS256"

_discovery: Dict[str, Any] = {}
_discovery_at = 0.0
_used_handoffs: Dict[str, float] = {}


class OIDCError(Exception):
    """A sign-in that cannot go on; the message is shown to the user."""


def enabled() -> bool:
    return bool(settings.OIDC_CLIENT_ID and settings.OIDC_CLIENT_SECRET
                and settings.OIDC_REDIRECT_URL)


def _key(purpose: str) -> str:
    # A key of its own for each purpose, so that neither the state cookie nor
    # the hand-off token can ever pass as a session token.
    return hashlib.sha256(f"{settings.SECRET_KEY}:oidc:{purpose}".encode()).hexdigest()


async def discovery() -> Dict[str, Any]:
    global _discovery, _discovery_at
    if _discovery and time.time() - _discovery_at < 3600:
        return _discovery
    url = settings.OIDC_ISSUER.rstrip("/") + "/.well-known/openid-configuration"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url)
        r.raise_for_status()
        _discovery, _discovery_at = r.json(), time.time()
    return _discovery


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


async def start() -> Dict[str, str]:
    """The provider URL to send the browser to, and the cookie that goes with it."""
    conf = await discovery()
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    params = {
        "response_type": "code",
        "client_id": settings.OIDC_CLIENT_ID,
        "redirect_uri": settings.OIDC_REDIRECT_URL,
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "code_challenge": _challenge(verifier),
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    url = str(httpx.URL(conf["authorization_endpoint"], params=params))
    cookie = jwt.encode({"state": state, "nonce": nonce, "verifier": verifier,
                         "exp": int(time.time()) + STATE_TTL}, _key("state"), algorithm=_ALG)
    return {"url": url, "cookie": cookie}


def _allowed_domain(email: str) -> bool:
    domains = [d.strip().lower() for d in settings.OIDC_ALLOWED_DOMAINS.split(",") if d.strip()]
    return not domains or email.rsplit("@", 1)[-1].lower() in domains


async def finish(code: Optional[str], state: Optional[str], cookie: Optional[str]) -> str:
    """Check the callback and return the verified email it is for."""
    if not code or not state or not cookie:
        raise OIDCError("The sign-in was interrupted. Please try again.")
    try:
        saved = jwt.decode(cookie, _key("state"), algorithms=[_ALG])
    except JWTError:
        raise OIDCError("The sign-in took too long. Please try again.")
    if not secrets.compare_digest(saved.get("state", ""), state):
        raise OIDCError("The sign-in could not be verified. Please try again.")

    conf = await discovery()
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(conf["token_endpoint"], data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.OIDC_REDIRECT_URL,
            "client_id": settings.OIDC_CLIENT_ID,
            "client_secret": settings.OIDC_CLIENT_SECRET,
            "code_verifier": saved["verifier"],
        })
    if r.status_code != 200 or "id_token" not in r.json():
        raise OIDCError(f"{settings.OIDC_LABEL} did not confirm the sign-in.")
    claims = jwt.get_unverified_claims(r.json()["id_token"])

    aud = claims.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    if (claims.get("iss", "").rstrip("/") != conf.get("issuer", settings.OIDC_ISSUER).rstrip("/")
            or settings.OIDC_CLIENT_ID not in auds
            or int(claims.get("exp", 0)) < time.time()
            or not secrets.compare_digest(str(claims.get("nonce", "")), saved["nonce"])):
        raise OIDCError("The sign-in could not be verified. Please try again.")
    email = str(claims.get("email") or "").strip()
    if not email or claims.get("email_verified") not in (True, "true"):
        raise OIDCError(f"{settings.OIDC_LABEL} did not give a verified email address.")
    if not _allowed_domain(email):
        raise OIDCError(f"Accounts at {email.rsplit('@', 1)[-1]} cannot sign in here.")
    return email


def handoff(username: str, version: int) -> str:
    return jwt.encode({"sub": username, "ver": version, "jti": secrets.token_urlsafe(12),
                       "exp": int(time.time()) + HANDOFF_TTL}, _key("handoff"), algorithm=_ALG)


def redeem(token: str) -> Dict[str, Any]:
    """The claims of a hand-off token, once; raises OIDCError otherwise."""
    try:
        claims = jwt.decode(token, _key("handoff"), algorithms=[_ALG])
    except JWTError:
        raise OIDCError("The sign-in expired. Please try again.")
    now = time.time()
    for jti, exp in list(_used_handoffs.items()):
        if exp < now:
            del _used_handoffs[jti]
    if claims["jti"] in _used_handoffs:
        raise OIDCError("The sign-in expired. Please try again.")
    _used_handoffs[claims["jti"]] = claims["exp"]
    return claims
