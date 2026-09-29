"""Reverse proxy for per-user JupyterLab servers.

The frontend never knows internal ports: it requests
``/jupyter/<username>/<rest>`` and this router forwards the request.  HTTP or
WebSocket, to the matching user's JupyterLab (a loopback port in *process*
mode, a Docker-network hostname in *container* mode).

Authorisation
-------------
Every request must carry one of:

* a valid platform JWT, as ``Authorization: Bearer``, as the
  ``gpu_proxy_token`` cookie (set at login, which is what plain browser
  navigation to ``/jupyter/<user>/`` uses), or as ``?platform_token=``;
* the user's own Jupyter ``ServerApp.token`` (legacy token links).

A JWT only grants access to its own ``sub``; admins may reach any user's
server.  Password-mode sessions are **not** exempt: Jupyter's own login form
is a second factor, never the only one.
"""

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass
from typing import Optional

import httpx
import websockets
from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

import models
from auth import get_current_user
from database import SessionLocal
from services import crypto, session_backend

logger = logging.getLogger(__name__)

router = APIRouter(tags=["proxy"])

# Name of the HttpOnly cookie carrying the platform JWT for browser
# navigation to /jupyter/* (set by routers.auth on login).
PROXY_COOKIE = "gpu_proxy_token"

# Shared async HTTP client (connection pooling)
_client: Optional[httpx.AsyncClient] = None

# last_activity is written at most once per user per this many seconds so the
# idle reaper has fresh data without one DB write per kernel message.
_ACTIVITY_THROTTLE_SECONDS = 60
_last_activity_write: dict = {}


@dataclass
class SessionView:
    """Detached snapshot of a Jupyter session row.

    A plain dataclass rather than a re-instantiated ORM object: the previous
    implementation passed a non-column keyword to ``models.JupyterSession``,
    which SQLAlchemy's declarative constructor rejects with ``TypeError``.  Every
    single /jupyter request returned 500.
    """

    user_id: int
    username: str
    port: int
    pid: Optional[int]
    container_id: Optional[str]
    status: models.SessionStatus
    token: str
    base_url: str
    # True only when the user CHOSE a Jupyter password.  A password derived
    # from their account password is not a second factor; it exists so the
    # login form accepts something they already know, so the proxy still
    # vouches for them and they are never prompted twice.
    has_jupyter_password: bool


async def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(120.0))
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _find_session(username: str) -> Optional[SessionView]:
    """Open a short-lived DB session to resolve the user's Jupyter session."""
    db = SessionLocal()
    try:
        user = db.query(models.User).filter(models.User.username == username).first()
        if user is None or user.deleted_at is not None:
            return None
        session = (
            db.query(models.JupyterSession)
            .filter(models.JupyterSession.user_id == user.id)
            .first()
        )
        if session is None:
            return None
        return SessionView(
            user_id=session.user_id,
            username=user.username,
            port=session.port,
            pid=session.pid,
            container_id=session.container_id,
            status=session.status,
            # Stored encrypted at rest (services.crypto)
            token=crypto.decrypt(session.token) or "",
            base_url=session.base_url,
            has_jupyter_password=bool(user.hashed_jupyter_password),  # user-chosen only
        )
    finally:
        db.close()


def touch_activity(user_id: int) -> None:
    """Record proxy traffic as user activity (throttled, best-effort).

    The idle reaper (services.reaper) stops sessions whose ``last_activity``
    is older than IDLE_TIMEOUT_MINUTES, without this call the column stayed
    at its creation time forever and idle reaping was impossible.
    """
    now = time.monotonic()
    previous = _last_activity_write.get(user_id, 0.0)
    if now - previous < _ACTIVITY_THROTTLE_SECONDS:
        return
    _last_activity_write[user_id] = now

    from datetime import datetime

    db = SessionLocal()
    try:
        db.query(models.JupyterSession).filter(
            models.JupyterSession.user_id == user_id
        ).update({"last_activity": datetime.utcnow()})
        db.commit()
    except Exception as exc:  # noqa: BLE001 (never break a proxied request)
        logger.debug("last_activity update failed for user_id=%s: %s", user_id, exc)
        db.rollback()
    finally:
        db.close()


def _jwt_grants_access(jwt_token: str, username: str) -> bool:
    db = SessionLocal()
    try:
        user = get_current_user(token=jwt_token, db=db)
        return user is not None and (user.username == username or user.is_admin)
    except Exception:  # noqa: BLE001 (invalid / expired / revoked JWT)
        return False
    finally:
        db.close()


def _authorized(
    username: str, session: SessionView, request_headers, query_params, cookies
) -> Optional[str]:
    """How the caller is allowed to reach this user's Jupyter, or None.

    Returns ``"jupyter"`` when they already hold the upstream's own token,
    ``"platform"`` when a platform JWT vouched for them, ``None`` when neither.
    The distinction matters: a platform-authenticated caller has no Jupyter
    credential of their own, so the proxy has to supply one (see
    :func:`_upstream_auth_headers`).
    """
    # 1) The user's own Jupyter token (legacy ?token= links)
    supplied = query_params.get("token")
    if supplied and session.token and supplied == session.token:
        return "jupyter"

    # 2) Platform JWT, header, cookie (browser navigation) or query param (WS)
    auth_header = request_headers.get("authorization", "")
    candidates = []
    if auth_header.lower().startswith("bearer "):
        candidates.append(auth_header[7:])
    if query_params.get("platform_token"):
        candidates.append(query_params["platform_token"])
    if cookies.get(PROXY_COOKIE):
        candidates.append(cookies[PROXY_COOKIE])

    if any(_jwt_grants_access(tok, username) for tok in candidates if tok):
        return "platform"
    return None


def _upstream_auth_headers(session: SessionView, method: str, headers: dict) -> dict:
    """Give the upstream Jupyter a credential it actually understands.

    The platform's JWT means nothing to Jupyter, so a caller authenticated by
    the platform alone used to get 403 from Jupyter for every API call and
    "_xsrf argument missing" for every POST, the dashboard link worked only
    because it carried ?token= in the URL.

    * Token-mode session → inject ``Authorization: token <jupyter token>``.
      Header auth also exempts the request from Jupyter's XSRF check, which is
      what makes kernel creation work.
    * Password-mode session → inject nothing and drop the platform header:
      the user's own Jupyter password is a deliberate second factor, and
      Jupyter's login form issues the session cookie.
    """
    headers = {k: v for k, v in headers.items() if k.lower() != "authorization"}
    if method == "platform" and not session.has_jupyter_password and session.token:
        headers["Authorization"] = f"token {session.token}"
    elif method == "jupyter":
        headers["Authorization"] = f"token {session.token}"
    return headers


def _forwarded_query(query_params) -> dict:
    """Drop platform-only query parameters before forwarding upstream."""
    return {k: v for k, v in query_params.items() if k != "platform_token"}


def _hop_by_hop_headers(headers) -> dict:
    """Strip hop-by-hop headers before forwarding."""
    blocked = {
        "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailers", "transfer-encoding", "upgrade", "host",
        "content-length",
    }
    return {k: v for k, v in headers.items() if k.lower() not in blocked}


def _upstream(session: SessionView, username: str) -> str:
    """Base URL of the user's Jupyter, honouring how the session was launched."""
    backend_name = "container" if session.container_id else "process"
    return session_backend.target_base_url(username, session.port, backend_name)


# ---------------------------------------------------------------------------
# HTTP proxy
# ---------------------------------------------------------------------------

@router.api_route(
    "/jupyter/{username}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def proxy_jupyter(username: str, path: str, request: Request):
    session = _find_session(username)
    if session is None:
        return Response(status_code=404, content="No Jupyter session for this user")

    auth_method = _authorized(
        username, session, request.headers, request.query_params, request.cookies
    )
    if auth_method is None:
        return Response(status_code=403, content="Not authorized to access this Jupyter server")

    if session.status != models.SessionStatus.running:
        return Response(
            status_code=503,
            content="Jupyter server is not running. Start it from the dashboard.",
        )

    backend_base = _upstream(session, username)
    # base_url is /jupyter/<username>/, the upstream Jupyter was configured
    # with the same base_url, so the full original path maps 1:1.
    upstream_path = f"{session.base_url}{path}" if path else session.base_url
    target = f"{backend_base}{upstream_path}"
    client = await get_client()

    # Host is intentionally NOT pinned: httpx derives it from the target URL,
    # so container-mode requests get the container's own hostname instead of a
    # bogus 127.0.0.1:<platform port> that broke Jupyter's host checks.
    headers = _upstream_auth_headers(
        session, auth_method, _hop_by_hop_headers(request.headers)
    )
    body = await request.body()

    try:
        backend_response = await client.request(
            request.method,
            target,
            params=_forwarded_query(request.query_params),
            headers=headers,
            content=body if body else None,
            follow_redirects=False,
        )
    except httpx.ConnectError:
        return Response(status_code=502, content="Jupyter backend unreachable")
    except httpx.HTTPError as exc:
        logger.warning("Proxy error for %s%s: %s", username, upstream_path, exc)
        return Response(status_code=502, content="Jupyter backend error")

    await asyncio.to_thread(touch_activity, session.user_id)

    excluded = {"content-encoding", "content-length", "transfer-encoding", "connection"}
    response_headers = {
        k: v for k, v in backend_response.headers.items() if k.lower() not in excluded
    }

    # Rewrite absolute redirects pointing at the INTERNAL upstream so the
    # browser stays on the platform origin (critical for Jupyter's post-login
    # redirect in password mode).
    if backend_response.is_redirect:
        location = response_headers.get("location")
        if location:
            from urllib.parse import urlsplit

            parts = urlsplit(location)
            internal_host = urlsplit(backend_base).netloc
            if parts.netloc in {internal_host, f"127.0.0.1:{session.port}", f"localhost:{session.port}"}:
                response_headers["location"] = (
                    parts.path + ("?" + parts.query if parts.query else "")
                )

    return StreamingResponse(
        backend_response.aiter_bytes(),
        status_code=backend_response.status_code,
        headers=response_headers,
    )


# ---------------------------------------------------------------------------
# WebSocket proxy (kernel channels, terminals, LSP)
# ---------------------------------------------------------------------------

# websockets renamed the client's header kwarg (extra_headers → additional_headers
# in 14.0).  Resolve once so the proxy works across pinned versions.
_WS_HEADER_KWARG = (
    "additional_headers"
    if "additional_headers" in inspect.signature(websockets.connect).parameters
    else "extra_headers"
)

# Headers the upstream handshake must not receive verbatim, websockets builds
# its own.
_WS_SKIP_HEADERS = {
    "host", "connection", "upgrade", "sec-websocket-key", "sec-websocket-version",
    "sec-websocket-extensions", "sec-websocket-protocol", "sec-websocket-accept",
}


@router.websocket("/jupyter/{username}/{path:path}")
async def proxy_jupyter_ws(websocket: WebSocket, username: str, path: str):
    session = _find_session(username)
    if session is None:
        await websocket.close(code=4404)
        return

    auth_method = _authorized(
        username, session, websocket.headers, websocket.query_params, websocket.cookies
    )
    if auth_method is None:
        await websocket.close(code=4403)
        return

    if session.status != models.SessionStatus.running:
        await websocket.close(code=4404)
        return

    backend_base = _upstream(session, username)
    upstream_path = f"{session.base_url}{path}" if path else session.base_url
    query = "&".join(f"{k}={v}" for k, v in _forwarded_query(websocket.query_params).items())
    ws_target = f"{backend_base}{upstream_path}".replace("http://", "ws://", 1)
    if query:
        ws_target += f"?{query}"

    # Jupyter negotiates a kernel subprotocol (v1.kernel.websocket.jupyter.org);
    # it must be offered upstream and echoed back to the browser, otherwise the
    # kernel falls back to a protocol the client did not agree to.
    requested = websocket.headers.get("sec-websocket-protocol")
    subprotocols = [p.strip() for p in requested.split(",")] if requested else None

    headers = list(
        _upstream_auth_headers(
            session,
            auth_method,
            {
                k: v for k, v in websocket.headers.items()
                if k.lower() not in _WS_SKIP_HEADERS
            },
        ).items()
    )

    connect_kwargs = {
        _WS_HEADER_KWARG: headers,
        "subprotocols": subprotocols,
        "open_timeout": 20,
        "ping_interval": 20,
        "ping_timeout": 20,
        "max_size": None,          # notebooks push large outputs
        "close_timeout": 5,
    }

    try:
        upstream = await websockets.connect(ws_target, **connect_kwargs)
    except Exception as exc:  # noqa: BLE001 (upstream refused / not ready)
        logger.warning("WS upstream connect failed for %s: %s", ws_target, exc)
        await websocket.close(code=1011)
        return

    await websocket.accept(subprotocol=upstream.subprotocol)
    await asyncio.to_thread(touch_activity, session.user_id)

    async def client_to_upstream() -> None:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return
            if message.get("text") is not None:
                await upstream.send(message["text"])
            elif message.get("bytes") is not None:
                await upstream.send(message["bytes"])

    async def upstream_to_client() -> None:
        async for message in upstream:
            if isinstance(message, str):
                await websocket.send_text(message)
            else:
                await websocket.send_bytes(message)

    pump_client = asyncio.create_task(client_to_upstream())
    pump_upstream = asyncio.create_task(upstream_to_client())
    try:
        done, pending = await asyncio.wait(
            {pump_client, pump_upstream}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001 (either side vanished)
        logger.debug("WS proxy for %s ended: %s", username, exc)
    finally:
        await upstream.close()
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001 (already closed)
            pass
