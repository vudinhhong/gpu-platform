"""The master's view of its nodes, and the channel it talks to them over.

A node is a machine whose GPUs and CPUs the scheduler may use.  The master is
one (``is_local``); every other node joins with a single-use token and from
then on opens every connection itself (docs/multi-node.md):

* it **posts heartbeats**: what its GPUs hold and what its containers use,
  which the scheduler and the VRAM guard read where they would read the local
  scan;
* it **long-polls for commands** and posts each result back.

``call(node, op, **args)`` is how the rest of the backend asks a node to do
something.  On the local node it calls the function directly; on another it
queues the command and waits for the answer.  Both ends run the same
functions (services.node_ops), which is what keeps one node and several the
same code.

State lives in this process: the backend runs one worker (see the Dockerfile),
and a command still in flight when it restarts is simply lost, which the
caller sees as a timeout, the same as a node that went away.
"""

import hashlib
import hmac
import itertools
import json
import logging
import secrets
import socket
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import models
from config import settings

logger = logging.getLogger(__name__)


class NodeError(RuntimeError):
    """A command could not be carried out on a node (or it did not answer)."""


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------

def local_node(db) -> "models.Node":
    """The master's own row, created on first use."""
    node = db.query(models.Node).filter(models.Node.is_local.is_(True)).first()
    if node is not None and settings.NODE_NAME and node.name != settings.NODE_NAME \
            and not db.query(models.Node).filter(models.Node.name == settings.NODE_NAME).first():
        # Named after the setting once it exists; a row created before it was
        # set carries the container's host name, which tells nobody anything.
        node.name = settings.NODE_NAME
        db.commit()
    if node is None:
        name = settings.NODE_NAME or socket.gethostname()
        clash = db.query(models.Node).filter(models.Node.name == name).first()
        if clash is not None:
            name = f"{name}-master"
        node = models.Node(name=name, site=settings.NODE_SITE or "default", is_local=True,
                           state="ready", last_seen=datetime.utcnow())
        db.add(node)
        db.commit()
        db.refresh(node)
    return node


def node_for(db, node_id: Optional[int]) -> "models.Node":
    """A job's or workspace's node: NULL means the master, as it always did."""
    if node_id is None:
        return local_node(db)
    node = db.query(models.Node).filter(models.Node.id == node_id).first()
    if node is None:
        raise NodeError(f"node {node_id} does not exist")
    return node


def create_join_token(db, site: str = "default", actor: Optional[str] = None) -> Tuple[str, datetime]:
    token = "gpj_" + secrets.token_urlsafe(24)
    expires = datetime.utcnow() + timedelta(seconds=settings.JOIN_TOKEN_TTL_SECONDS)
    db.add(models.JoinToken(token_hash=_hash(token), site=site or "default",
                            created_by=actor, expires_at=expires))
    db.commit()
    return token, expires


def join(db, token: str, name: str, inventory: Dict[str, Any],
         data_url: Optional[str] = None) -> Tuple["models.Node", str, str]:
    """Spend *token* and admit the machine as node *name*.

    Returns the node, its secret and the key the master signs proxied
    requests with.

    Joining again under the same name (a reinstalled machine) replaces the
    secret, which is how a lost credential is recovered: a fresh token from an
    administrator, not anything the node can do alone.
    """
    row = db.query(models.JoinToken).filter(models.JoinToken.token_hash == _hash(token or "")).first()
    if row is None or row.used_at is not None or row.expires_at < datetime.utcnow():
        raise NodeError("the join token is unknown, used or expired")
    name = (name or "").strip()
    if not name or len(name) > 128:
        raise NodeError("a node needs a name of at most 128 characters")
    node = db.query(models.Node).filter(models.Node.name == name).first()
    if node is not None and node.is_local:
        raise NodeError(f"{name!r} is the master's own name")
    from services import crypto

    secret = secrets.token_urlsafe(32)
    proxy_key = secrets.token_urlsafe(32)
    if node is None:
        node = models.Node(name=name)
        db.add(node)
    node.site = row.site
    node.state = "ready"
    node.secret_hash = _hash(secret)
    node.data_url = (data_url or "").rstrip("/") or None
    node.proxy_key = crypto.encrypt(proxy_key)
    node.inventory = json.dumps(inventory or {})
    node.joined_at = node.last_seen = datetime.utcnow()
    db.flush()
    row.used_at = datetime.utcnow()
    row.node_id = node.id
    db.commit()
    db.refresh(node)
    logger.info("Node %r joined site %r", node.name, node.site)
    return node, secret, proxy_key


def authenticate(db, header: Optional[str]) -> Optional["models.Node"]:
    """The node an ``Authorization: Node <id>:<secret>`` header proves to be."""
    if not header or not header.startswith("Node "):
        return None
    ident, _, secret = header[5:].partition(":")
    if not ident.isdigit() or not secret:
        return None
    node = db.query(models.Node).filter(models.Node.id == int(ident)).first()
    if node is None or node.is_local or node.state == "removed" or not node.secret_hash:
        return None
    if not secrets.compare_digest(node.secret_hash, _hash(secret)):
        return None
    return node


# ---------------------------------------------------------------------------
# Proxied requests
# ---------------------------------------------------------------------------
# The master reaches a workspace on another node through that node's data port
# (NODE_DATA_URL).  The port answers only requests the master signed for one
# user, with a timestamp, so a captured header is good for a minute and for
# that user's workspace alone.

PROXY_HEADER = "X-Node-Proxy"
PROXY_MAX_AGE = 60


def _proxy_sig(key: str, ts: str, username: str) -> str:
    return hmac.new(key.encode(), f"{ts}:{username}".encode(), hashlib.sha256).hexdigest()


def proxy_header(node: "models.Node", username: str) -> Dict[str, str]:
    from services import crypto

    key = crypto.decrypt(node.proxy_key)
    if not key:
        raise NodeError(f"node {node.name!r} has no proxy key; join it again")
    ts = str(int(time.time()))
    return {PROXY_HEADER: f"{ts}.{_proxy_sig(key, ts, username)}"}


def proxy_allowed(key: Optional[str], header: Optional[str], username: str) -> bool:
    if not key or not header or "." not in header:
        return False
    ts, _, sig = header.partition(".")
    if not ts.isdigit() or abs(time.time() - int(ts)) > PROXY_MAX_AGE:
        return False
    return hmac.compare_digest(sig, _proxy_sig(key, ts, username))


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------

_telemetry: Dict[int, Dict[str, Any]] = {}
_last_written: Dict[int, float] = {}


def record_heartbeat(db, node: "models.Node", payload: Dict[str, Any]) -> None:
    _telemetry[node.id] = {**payload, "received": time.time()}
    # The row's last_seen is for people and for restarts; the scheduler reads
    # the in-memory figure, so the row need not be written every two seconds.
    if time.time() - _last_written.get(node.id, 0) > 15:
        node.last_seen = datetime.utcnow()
        db.add(node)
        db.commit()
        _last_written[node.id] = time.time()


def telemetry(node_id: int) -> Optional[Dict[str, Any]]:
    """The node's last heartbeat, or None if it is older than NODE_STALE_SECONDS."""
    t = _telemetry.get(node_id)
    if t is None or time.time() - t["received"] > settings.NODE_STALE_SECONDS:
        return None
    return t


def is_ready(node: "models.Node") -> bool:
    """May new work be placed on *node*?"""
    if node.state != "ready":
        return False
    return node.is_local or telemetry(node.id) is not None


def ready_nodes(db, site: Optional[str] = None) -> List["models.Node"]:
    local_node(db)
    q = db.query(models.Node).filter(models.Node.state == "ready")
    if site:
        q = q.filter(models.Node.site == site)
    return [n for n in q.order_by(models.Node.id).all() if is_ready(n)]


def site_of(user) -> str:
    return getattr(user, "site", None) or settings.NODE_SITE or "default"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

_lock = threading.Condition()
_queues: Dict[int, deque] = {}
_waiting: Dict[int, Dict[str, Any]] = {}
_ids = itertools.count(1)
# The boot id of each node's agent as of its latest poll.  A poll whose
# agent has since restarted is still parked here, its connection dead: what
# it takes would be lost, and whoever sent it would wait out the timeout.
# The scheduler pass waited 120 s that way, every job with it.
_boots: Dict[int, str] = {}


def call(db, node_id: Optional[int], op: str, timeout: Optional[float] = None, **args) -> Any:
    """Run node operation *op* with *args* on the node and return its result."""
    from services import node_ops

    node = node_for(db, node_id)
    if node.is_local:
        return node_ops.run(op, args)
    if telemetry(node.id) is None:
        raise NodeError(f"node {node.name!r} is unreachable")

    cmd_id = next(_ids)
    slot = {"event": threading.Event(), "ok": None, "value": None, "node": node.id}
    with _lock:
        _waiting[cmd_id] = slot
        _queues.setdefault(node.id, deque()).append({"id": cmd_id, "op": op, "args": args})
        _lock.notify_all()
    if not slot["event"].wait(timeout or settings.NODE_COMMAND_TIMEOUT_SECONDS):
        with _lock:
            _waiting.pop(cmd_id, None)
            queue = _queues.get(node.id)
            if queue:
                for queued in list(queue):
                    if queued["id"] == cmd_id:
                        queue.remove(queued)
        raise NodeError(f"node {node.name!r} did not answer {op} in time")
    if not slot["ok"]:
        raise NodeError(f"{op} on node {node.name!r} failed: {slot['value']}")
    return slot["value"]


def take_commands(node_id: int, wait: float, boot: Optional[str] = None) -> List[Dict[str, Any]]:
    """Commands for the node, waiting up to *wait* seconds for the first.

    *boot* identifies the agent's run.  When it changes, the commands handed
    to the earlier run and not answered are failed at once; and a poll from an
    earlier run gets nothing more.
    """
    deadline = time.time() + wait
    with _lock:
        if boot is not None and _boots.get(node_id) != boot:
            if node_id in _boots:
                _fail_handed_out(node_id, "the node's agent restarted before answering")
            _boots[node_id] = boot
            _lock.notify_all()           # the earlier run's parked poll leaves
        while not _queues.get(node_id):
            left = deadline - time.time()
            if left <= 0 or (boot is not None and _boots.get(node_id) != boot):
                return []
            _lock.wait(left)
        if boot is not None and _boots.get(node_id) != boot:
            return []
        queue = _queues[node_id]
        out = list(queue)
        queue.clear()
        for cmd in out:
            slot = _waiting.get(cmd["id"])
            if slot is not None:
                slot["handed_to"] = boot
        return out


def _fail_handed_out(node_id: int, reason: str) -> None:
    """Fail every command handed to *node_id* and not yet answered (lock held)."""
    for cmd_id, slot in list(_waiting.items()):
        if slot["node"] == node_id and "handed_to" in slot:
            _waiting.pop(cmd_id)
            slot["ok"], slot["value"] = False, reason
            slot["event"].set()


def deliver_result(node_id: int, cmd_id: int, ok: bool, value: Any) -> bool:
    with _lock:
        slot = _waiting.get(cmd_id)
        # A node answers only for commands sent to it.
        if slot is None or slot["node"] != node_id:
            return False                 # timed out already, or not its own
        _waiting.pop(cmd_id)
    slot["ok"], slot["value"] = bool(ok), value
    slot["event"].set()
    return True


def _reset_for_tests() -> None:
    with _lock:
        _queues.clear()
        _waiting.clear()
        _boots.clear()
    _telemetry.clear()
    _last_written.clear()
