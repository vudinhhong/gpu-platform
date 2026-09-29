"""GPU + resource monitoring endpoints (REST + WebSocket)."""

import asyncio
import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import Session

import models
from auth import get_current_user
from database import SessionLocal, get_db
from services import gpu_monitor, metrics

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/gpu", tags=["gpu"])


def _allowed_indices(user: models.User) -> List[int]:
    assignment = user.gpu_assignment
    if assignment is None:
        return []
    return [
        int(x)
        for x in assignment.gpu_indices.split(",")
        if x.strip().lstrip("-").isdigit()
    ]


def _payload_for(user: models.User) -> Dict[str, Any]:
    """One frame of the live feed, scoped to what this user may see.

    Regular users get their own GPUs and their own container's CPU/RAM;
    admins get the whole host.  Previously the WebSocket pushed every GPU,
    including other users' process lists, to anyone logged in.
    """
    snapshot = metrics.latest()
    if user.is_admin:
        return {
            "gpus": gpu_monitor.get_gpu_status(detailed=True),
            "host": snapshot.get("host", {}),
            "containers": snapshot.get("containers", []),
            "collected_at": snapshot.get("collected_at"),
            "scope": "admin",
        }

    indices = _allowed_indices(user)
    return {
        "gpus": gpu_monitor.get_gpu_status_for(indices) if indices else [],
        "host": {},
        "containers": [c for c in snapshot.get("containers", []) if c["user"] == user.username],
        "collected_at": snapshot.get("collected_at"),
        "scope": "user",
    }


@router.get("/status", response_model=List[Dict[str, Any]])
async def gpu_status(current_user: models.User = Depends(get_current_user)):
    """Live status of every physical GPU (admins) or the caller's own GPUs."""
    if current_user.is_admin:
        return gpu_monitor.get_gpu_status(detailed=True)
    indices = _allowed_indices(current_user)
    return gpu_monitor.get_gpu_status_for(indices) if indices else []


@router.get("/mine", response_model=List[Dict[str, Any]])
async def my_gpu_status(current_user: models.User = Depends(get_current_user)):
    """Live status restricted to the GPUs assigned to the current user."""
    if current_user.is_admin:
        return gpu_monitor.get_gpu_status(detailed=True)
    indices = _allowed_indices(current_user)
    return gpu_monitor.get_gpu_status_for(indices) if indices else []


@router.get("/resources")
async def resource_snapshot(current_user: models.User = Depends(get_current_user)):
    """Single-shot version of the WebSocket frame (for clients without WS)."""
    return _payload_for(current_user)


@router.websocket("/ws")
async def gpu_ws(websocket: WebSocket):
    """Push GPU + resource state to connected clients every few seconds.

    The client passes its JWT as ``?token=<jwt>``; we authenticate before
    accepting.  nvidia-smi is blocking, so each frame is built in a worker
    thread rather than on the event loop.
    """
    token = websocket.query_params.get("token", "")
    db = SessionLocal()
    try:
        try:
            user = get_current_user(token=token, db=db)
            username = user.username
        except Exception:  # noqa: BLE001 (invalid/expired/revoked token)
            await websocket.close(code=4401)
            return
    finally:
        db.close()

    await websocket.accept()
    try:
        while True:
            # Re-read the user each frame so a revoked or deactivated account
            # stops receiving data without waiting for a reconnect.
            db = SessionLocal()
            try:
                user = db.query(models.User).filter(models.User.username == username).first()
                if user is None or not user.is_active:
                    await websocket.close(code=4401)
                    return
                payload = await asyncio.to_thread(_payload_for, user)
            finally:
                db.close()
            await websocket.send_json(payload)
            await asyncio.sleep(3)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001 (client vanished mid-send)
        logger.debug("GPU websocket for %s ended: %s", username, exc)
