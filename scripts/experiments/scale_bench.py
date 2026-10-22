#!/usr/bin/env python3
"""How the scheduler pass and the queue API scale with users and jobs.

Runs the backend in-process against a throwaway SQLite database (the same
set-up as the test suite: no Docker, no GPUs), fills it with N users, a usage
history for each, and M queued jobs, and times:

    fairshare.scores       the score computation a pass starts with
    dispatch pass          _dispatch_queued over the whole queue, with no GPU
                           free, so every queued job is considered and skipped
                           (the most expensive pass there is)
    blockers               the "why is my job waiting" replay of the dispatcher
    GET /api/jobs/queue    the shared queue, as a user fetches it

Run inside the backend image so the dependencies are there:

    docker run --rm -v "$PWD/backend:/src:ro" -v "$PWD/scripts/experiments:/exp:ro" \\
        --entrypoint sh workspace-gpu-backend -c \\
        'cp -r /src /b && cd /b && python /exp/scale_bench.py' > paper/data/scale.json
"""

import json
import os
import statistics
import sys
import tempfile
import time

_DB = os.path.join(tempfile.mkdtemp(), "bench.db")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"
os.environ["JUPYTER_DATA_DIR"] = tempfile.mkdtemp()
os.environ["ALLOW_MOCK_GPU"] = "true"
os.environ["SECRET_KEY"] = "bench-secret-key-not-used-anywhere-0123456789abcdef"
sys.path.insert(0, os.getcwd())

from datetime import datetime, timedelta  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import models  # noqa: E402
from auth import get_password_hash  # noqa: E402
from database import SessionLocal, engine  # noqa: E402
from services import fairshare, jobs as job_service, quota  # noqa: E402

REPS = 5


def reset():
    models.Base.metadata.drop_all(bind=engine)
    models.Base.metadata.create_all(bind=engine)


def populate(n_users, n_queued, history_per_user=50):
    db = SessionLocal()
    now = datetime.utcnow()
    pw = get_password_hash("Bench-pass-1")
    users = [models.User(username=f"u{i}", email=f"u{i}@b", hashed_password=pw)
             for i in range(n_users)]
    db.add_all(users)
    db.commit()
    for u in users:
        db.refresh(u)
    rows = []
    for u in users:
        for k in range(history_per_user):
            start = now - timedelta(hours=48) + timedelta(minutes=k * 50)
            rows.append(models.UsageRecord(
                user_id=u.id, username=u.username, gpu_count=1, cpu_cores=4,
                backend="job", started_at=start, ended_at=start + timedelta(minutes=30),
                gpu_seconds=1800, cpu_seconds=7200))
    db.add_all(rows)
    jobs = []
    for k in range(n_queued):
        u = users[k % n_users]
        jobs.append(models.Job(user_id=u.id, username=u.username, script="j.sh",
                               workdir="", gpu_count=1, gpu_memory_mb=20000,
                               status=models.JobStatus.queued,
                               created_at=now + timedelta(seconds=k)))
    db.add_all(jobs)
    db.commit()
    first = users[0].username
    db.close()
    return first


def timed(fn, reps=REPS):
    out = []
    for _ in range(reps):
        t0 = time.perf_counter()
        fn()
        out.append((time.perf_counter() - t0) * 1000)
    return {"median_ms": statistics.median(out), "max_ms": max(out)}


def main_bench():
    # Nothing is free on the (mock) cards, so every queued job is considered.
    job_service.gpu_availability = lambda db: [
        {"index": i, "uuid": None, "name": "mock", "total_mb": 49140, "used_mb": 49140,
         "reserved_mb": 0, "entitled_mb": 0, "free_mb": 0, "running_jobs": 0}
        for i in (0, 1)]
    job_service.cpu_capacity = lambda db: {"total_cores": 28, "committed_cores": 0,
                                           "free_cores": 28, "basis": "measured"}
    quota.job_blocker = lambda db, user: None

    results = []
    with TestClient(main.app) as client:
        for n_users in (10, 50, 200):
            for n_queued in (10, 100, 1000):
                reset()
                username = populate(n_users, n_queued)
                res = client.post("/api/auth/login",
                                  data={"username": username, "password": "Bench-pass-1"})
                token = res.json()["access_token"]
                hdr = {"Authorization": f"Bearer {token}"}

                def scores():
                    db = SessionLocal()
                    try:
                        fairshare.scores(db)
                    finally:
                        db.close()

                def dispatch():
                    db = SessionLocal()
                    try:
                        job_service._dispatch_queued(db)
                    finally:
                        db.close()

                def blockers():
                    db = SessionLocal()
                    try:
                        job_service.queued_blockers(db)
                    finally:
                        db.close()

                def api_queue():
                    r = client.get("/api/jobs/queue", headers=hdr)
                    assert r.status_code == 200, r.text

                row = {"users": n_users, "queued": n_queued,
                       "scores": timed(scores), "dispatch_pass": timed(dispatch),
                       "blockers": timed(blockers), "api_queue": timed(api_queue)}
                print(json.dumps(row), file=sys.stderr, flush=True)
                results.append(row)
    return results


if __name__ == "__main__":
    out = {"generated": datetime.now().astimezone().isoformat(timespec="seconds"),
           "cpu": os.cpu_count(), "reps": REPS, "rows": main_bench()}
    print(json.dumps(out, indent=1))
