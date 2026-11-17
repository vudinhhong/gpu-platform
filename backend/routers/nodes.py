"""Nodes: the administrator's view, and the channel the agents use.

Administrators list nodes, issue join tokens and drain, resume or remove a
node.  Agents join with a token and then authenticate every request with
``Authorization: Node <id>:<secret>``; nothing an agent sends is trusted
beyond its own node (docs/multi-node.md).
"""

import json
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

import models
from auth import require_admin
from database import get_db
from services import audit, nodes

admin_router = APIRouter(prefix="/api/admin/nodes", tags=["nodes"],
                         dependencies=[Depends(require_admin)])
agent_router = APIRouter(prefix="/api/nodes", tags=["nodes"])


def _view(node: "models.Node") -> Dict[str, Any]:
    t = nodes.telemetry(node.id)
    try:
        inventory = json.loads(node.inventory or "{}")
    except ValueError:
        inventory = {}
    return {
        "id": node.id, "name": node.name, "site": node.site, "is_local": node.is_local,
        "state": node.state, "ready": nodes.is_ready(node), "data_url": node.data_url,
        "joined_at": node.joined_at, "last_seen": node.last_seen,
        "heartbeat_age_s": None if t is None else round(time.time() - t["received"], 1),
        "inventory": inventory,
        "gpus": (t or {}).get("gpus"),
    }


# ── administrators ──────────────────────────────────────────────────────────

@admin_router.get("")
def list_nodes(db: Session = Depends(get_db)):
    nodes.local_node(db)
    rows = db.query(models.Node).filter(models.Node.state != "removed").order_by(models.Node.id).all()
    return [_view(n) for n in rows]


@admin_router.post("/join-tokens", status_code=status.HTTP_201_CREATED)
def new_join_token(request: Request, payload: Dict[str, Any] = Body(default={}),
                   db: Session = Depends(get_db), admin: models.User = Depends(require_admin)):
    site = str(payload.get("site") or "default").strip()[:64] or "default"
    token, expires = nodes.create_join_token(db, site=site, actor=admin.username)
    audit.record(db, "node.join_token", actor=admin.username, target=site,
                 ip_address=audit.client_ip(request))
    return {"token": token, "site": site, "expires_at": expires}


def _admin_node(db, node_id: int) -> "models.Node":
    node = db.query(models.Node).filter(models.Node.id == node_id).first()
    if node is None or node.state == "removed":
        raise HTTPException(status_code=404, detail="No such node.")
    return node


@admin_router.post("/{node_id}/drain")
def drain(node_id: int, request: Request, db: Session = Depends(get_db),
          admin: models.User = Depends(require_admin)):
    """No new work on the node; what runs there keeps running."""
    node = _admin_node(db, node_id)
    node.state = "draining"
    db.add(node)
    audit.record(db, "node.drain", actor=admin.username, target=node.name,
                 ip_address=audit.client_ip(request))
    return _view(node)


@admin_router.post("/{node_id}/resume")
def resume(node_id: int, request: Request, db: Session = Depends(get_db),
           admin: models.User = Depends(require_admin)):
    node = _admin_node(db, node_id)
    node.state = "ready"
    db.add(node)
    audit.record(db, "node.resume", actor=admin.username, target=node.name,
                 ip_address=audit.client_ip(request))
    return _view(node)


@admin_router.delete("/{node_id}")
def remove(node_id: int, request: Request, db: Session = Depends(get_db),
           admin: models.User = Depends(require_admin)):
    """Forget a node.  Refused while jobs are on it: drain it and let them end."""
    node = _admin_node(db, node_id)
    if node.is_local:
        raise HTTPException(status_code=400, detail="The master cannot be removed.")
    active = db.query(models.Job).filter(
        models.Job.node_id == node.id,
        models.Job.status.in_([models.JobStatus.starting, models.JobStatus.running,
                               models.JobStatus.paused])).count()
    if active:
        raise HTTPException(status_code=409, detail=f"{active} job(s) still on {node.name}; drain it first.")
    node.state = "removed"
    node.secret_hash = None
    db.add(node)
    audit.record(db, "node.remove", actor=admin.username, target=node.name,
                 ip_address=audit.client_ip(request))
    return {"message": f"Node {node.name} removed."}


# ── agents ──────────────────────────────────────────────────────────────────

def _node(db: Session = Depends(get_db), authorization: Optional[str] = Header(default=None)):
    node = nodes.authenticate(db, authorization)
    if node is None:
        raise HTTPException(status_code=401, detail="Not a node of this master.")
    return node


@agent_router.post("/join")
def join(request: Request, payload: Dict[str, Any] = Body(...), db: Session = Depends(get_db)):
    try:
        node, secret, proxy_key = nodes.join(db, str(payload.get("token") or ""), str(payload.get("name") or ""),
                                  payload.get("inventory") or {}, payload.get("data_url"))
    except nodes.NodeError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    audit.record(db, "node.join", actor=f"node:{node.name}", target=node.site,
                 ip_address=audit.client_ip(request))
    return {"node_id": node.id, "secret": secret, "proxy_key": proxy_key, "site": node.site}


@agent_router.post("/heartbeat")
def heartbeat(payload: Dict[str, Any] = Body(...), db: Session = Depends(get_db),
              node: "models.Node" = Depends(_node)):
    nodes.record_heartbeat(db, node, payload)
    return {"state": node.state}


@agent_router.get("/commands")
def commands(wait: float = Query(25.0, ge=0, le=55),
             boot: Optional[str] = Query(None, max_length=64),
             node: "models.Node" = Depends(_node)):
    # A plain (threadpool) handler: the wait blocks one worker thread per node,
    # which for a lab's handful of nodes is nothing.
    return {"commands": nodes.take_commands(node.id, wait, boot)}


@agent_router.post("/results")
def results(payload: Dict[str, Any] = Body(...), node: "models.Node" = Depends(_node)):
    accepted = nodes.deliver_result(node.id, int(payload.get("id") or 0),
                                    bool(payload.get("ok")), payload.get("value"))
    return {"accepted": accepted}
