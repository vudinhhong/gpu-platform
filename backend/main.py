"""GPU Platform: the FastAPI application entry point.

Wires together configuration, database, authentication, routers, the Jupyter
reverse-proxy and the background loops (self-heal, metrics, idle reaping).

Run locally with::

    uvicorn main:app --reload --port 8000
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import models  # noqa: F401  (ensures ORM models register on Base)
from config import settings
from database import SessionLocal, engine
from routers import admin, auth, gpu, jobs, proxy, user

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _secure_data_directory() -> None:
    """The SQLite DB holds password hashes and encrypted secrets, so: 0700."""
    data_dir = "data"
    if os.path.isdir(data_dir):
        try:
            os.chmod(data_dir, 0o700)
            db_path = os.path.join(data_dir, "gpu_platform.db")
            if os.path.exists(db_path):
                os.chmod(db_path, 0o600)
        except OSError as exc:  # pragma: no cover
            logger.warning("Could not secure data directory: %s", exc)


def _warn_on_weak_config() -> None:
    """Refuse to be quietly insecure: shout about defaults that matter."""
    if settings.SECRET_KEY.startswith("change-this"):
        logger.error(
            "SECRET_KEY is still the built-in default, so every JWT this platform "
            "issues is forgeable. Set SECRET_KEY in .env to a random 32+ byte value."
        )
    if settings.ADMIN_PASSWORD == "admin123":
        logger.warning(
            "ADMIN_PASSWORD is the documented default (admin123). Change it in "
            ".env and restart, or change the password after first login."
        )
    if settings.ALLOW_MOCK_GPU:
        logger.warning("ALLOW_MOCK_GPU=true, so the dashboard may show simulated GPUs.")


# ---------------------------------------------------------------------------
# Background loops
# ---------------------------------------------------------------------------

async def _periodic(name: str, interval: int, work) -> None:
    """Run *work* (a blocking callable) every *interval* seconds, forever.

    Every loop body runs in a worker thread: the Docker SDK and nvidia-smi are
    blocking, and running them on the event loop froze every API request.
    """
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(interval)
        try:
            await loop.run_in_executor(None, work)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 (a loop must never die)
            logger.error("%s loop iteration failed: %s", name, exc)


def _self_heal_pass() -> None:
    """Restart user containers that died unexpectedly (incl. OOM kills)."""
    from services import session_backend

    db = SessionLocal()
    try:
        running = (
            db.query(models.JupyterSession)
            .filter(models.JupyterSession.status == models.SessionStatus.running)
            .all()
        )
        if not running:
            return
        for action in session_backend.self_heal(running):
            logger.warning("Self-heal: %s", action)
    finally:
        db.close()


def _metrics_pass() -> None:
    from services import metrics

    metrics.refresh()


def _gpu_guard_pass() -> None:
    """Sample VRAM for running jobs and stop any past its allowance."""
    from services import jobs as job_service

    result = job_service.gpu_guard_pass()
    for job_id in result.get("stopped", []):
        logger.info("GPU guard stopped job %s for overrunning its reservation",
                    job_id)


def _scheduler_pass() -> None:
    """Finalise finished jobs and start queued ones that now fit."""
    from services import jobs as job_service

    result = job_service.scheduler_pass()
    for started in result.get("started", []):
        logger.info("Job scheduler started %s", started)


def _reaper_pass() -> None:
    """Reclaim idle sessions and act on disk-quota overruns."""
    from services import reaper

    reaper.reap_idle()
    reaper.enforce_disk_quota()


def _budget_pass() -> None:
    """Refresh what each running workspace reports about its owner's budget.

    On its own loop rather than inside the reaper: this one is a handful of SQL
    rows per running session, while the reaper beside it runs `du` over every
    workspace.  The budgets themselves are applied in the job scheduler, which
    is where the work they cover is admitted.
    """
    from services import reaper

    reaper.publish_time_budgets()


def _sync_authorized_keys() -> None:
    """Re-install every registered SSH key into the users' home volumes.

    Keys registered before the live-install path existed (or while the backend
    was down) are only in the database; their owners get a password prompt with
    no clue why.  One pass at startup makes the filesystem match the database.
    """
    from services import container_manager

    db = SessionLocal()
    try:
        installed = 0
        for user in db.query(models.User).filter(models.User.deleted_at.is_(None)).all():
            if not os.path.isdir(os.path.join(settings.JUPYTER_DATA_DIR, user.username)):
                continue  # user never started a session, nothing to sync into
            if container_manager.install_authorized_keys(user.username, user.ssh_public_key):
                if user.ssh_public_key:
                    installed += 1
        if installed:
            logger.info("Synced SSH authorized_keys for %d user(s)", installed)
    except Exception as exc:  # noqa: BLE001
        logger.error("authorized_keys sync failed: %s", exc)
    finally:
        db.close()


def _reconcile_startup() -> None:
    """Re-attach DB rows to whatever actually survived a backend restart."""
    from services import session_backend

    db = SessionLocal()
    try:
        sessions = db.query(models.JupyterSession).all()
        if not sessions:
            return
        corrections = session_backend.reconcile(sessions)
        if corrections:
            db.commit()
            logger.info("Startup reconcile: %s", corrections)
        else:
            logger.info("Startup reconcile: %d session(s) already consistent", len(sessions))
    except Exception as exc:  # noqa: BLE001
        logger.error("Startup reconcile failed: %s", exc)
        db.rollback()
    finally:
        db.close()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Create tables + seed the default admin on first boot.
    models.Base.metadata.create_all(bind=engine)

    _warn_on_weak_config()
    _secure_data_directory()

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _reconcile_startup)
    await loop.run_in_executor(None, _sync_authorized_keys)
    # Prime the metrics cache so the first dashboard load is not empty.
    await loop.run_in_executor(None, _metrics_pass)

    tasks = [
        asyncio.create_task(_periodic("self-heal", settings.SELF_HEAL_INTERVAL, _self_heal_pass)),
        asyncio.create_task(_periodic("metrics", settings.METRICS_INTERVAL_SECONDS, _metrics_pass)),
        asyncio.create_task(_periodic("idle-reaper", settings.REAPER_INTERVAL_SECONDS, _reaper_pass)),
        asyncio.create_task(_periodic(
            "job-scheduler", settings.JOB_SCHEDULER_INTERVAL, _scheduler_pass)),
        asyncio.create_task(_periodic(
            "gpu-guard", settings.GPU_GUARD_INTERVAL, _gpu_guard_pass)),
        asyncio.create_task(_periodic(
            "budget", settings.QUOTA_REFRESH_INTERVAL_SECONDS, _budget_pass)),
    ]

    yield

    for task in tasks:
        task.cancel()
    await proxy.close_client()

    # User sessions deliberately SURVIVE a backend restart.  Stopping them here
    # meant every platform update or container restart killed everyone's
    # running notebooks; the startup reconcile above re-attaches instead.
    if not settings.KEEP_SESSIONS_ON_SHUTDOWN:
        from services import session_backend, usage

        db = SessionLocal()
        try:
            for session in db.query(models.JupyterSession).all():
                session_backend.stop_session(
                    session.user.username, session.container_id
                )
                session.status = models.SessionStatus.stopped
                session.pid = None
                session.container_id = None
                usage.close_open_records(db, session.user, reason="shutdown", commit=False)
            db.commit()
        finally:
            db.close()


app = FastAPI(
    title="GPU Platform API",
    description="Multi-user GPU/CPU/RAM resource management with isolated JupyterLab sessions.",
    version="2.0.0",
    lifespan=lifespan,
)

# CORS – the SPA is normally served through the same origin (nginx), but the
# dev Vite server (localhost:3000) needs explicit permission.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ────────────────────────────────────────────────────────────────
app.include_router(auth.router)
app.include_router(user.router)
app.include_router(gpu.router)
app.include_router(jobs.router)
app.include_router(admin.router)

# ── Jupyter reverse proxy (HTTP + WebSocket), registered LAST so the
#    /jupyter/{username}/{path:path} catch-all never shadows /api routes.
app.include_router(proxy.router)


@app.get("/api/health", tags=["health"])
async def health_check():
    """Liveness probe used by the Docker healthcheck."""
    return {"status": "ok", "version": app.version}
