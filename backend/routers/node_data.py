"""A node's data port: the master's way in to the workspaces on this node.

Mounted only when ROLE=node.  The master proxies a user's Jupyter here as
``/_node/jupyter/<user>/...`` with a header it signed for that user
(services.nodes.proxy_header); anything else gets 403.  Users never talk to
this port, the master does, so it is the master that checks who the user is
and adds the Jupyter credential.  This end only forwards to the container.
"""

import logging

from fastapi import APIRouter, Request, Response, WebSocket

from routers import proxy
from services import container_manager, nodes

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/_node", tags=["node"])


def _key():
    from services import agent

    a = agent.current()
    return a.proxy_key if a is not None else None


def _base(username: str) -> str:
    return (f"http://{container_manager.container_hostname(username)}"
            f":{container_manager.CONTAINER_PORT}")


def _strip(headers) -> dict:
    return {k: v for k, v in headers.items() if k.lower() != nodes.PROXY_HEADER.lower()}


@router.api_route(
    "/jupyter/{username}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
)
async def forward(username: str, path: str, request: Request):
    if not nodes.proxy_allowed(_key(), request.headers.get(nodes.PROXY_HEADER), username):
        return Response(status_code=403)
    base = _base(username)
    from urllib.parse import urlsplit

    return await proxy.forward_http(
        request, f"{base}/jupyter/{username}/{path}",
        _strip(proxy._hop_by_hop_headers(request.headers)), request.query_params,
        internal_hosts=(urlsplit(base).netloc,),
    )


@router.websocket("/jupyter/{username}/{path:path}")
async def forward_ws(websocket: WebSocket, username: str, path: str):
    if not nodes.proxy_allowed(_key(), websocket.headers.get(nodes.PROXY_HEADER), username):
        await websocket.close(code=4403)
        return
    target = f"{_base(username)}/jupyter/{username}/{path}".replace("http://", "ws://", 1)
    query = "&".join(f"{k}={v}" for k, v in websocket.query_params.items())
    if query:
        target += f"?{query}"
    requested = websocket.headers.get("sec-websocket-protocol")
    subprotocols = [p.strip() for p in requested.split(",")] if requested else None
    headers = {k: v for k, v in _strip(websocket.headers).items()
               if k.lower() not in proxy._WS_SKIP_HEADERS}
    await proxy.forward_ws(websocket, target, headers, subprotocols)
