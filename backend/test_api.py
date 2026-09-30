"""End-to-end API tests (in-process, no real GPUs, Docker or Jupyter required).

Run with::

    python -m pytest test_api.py -v

Uses a temporary SQLite database and monkeypatches the Jupyter process manager
so the full user/admin workflow can be verified anywhere.
"""

import os
import sys
import tempfile

# Point SQLAlchemy at a throwaway DB *before* importing the app modules.
_TEST_DB = os.path.join(tempfile.gettempdir(), "gpu_platform_test.db")
if os.path.exists(_TEST_DB):
    os.remove(_TEST_DB)
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB}"
os.environ["JUPYTER_DATA_DIR"] = tempfile.mkdtemp(prefix="gpu_jupyter_test_")
# The suite has no GPUs; simulated ones keep the assignment paths exercisable.
os.environ["ALLOW_MOCK_GPU"] = "true"
os.environ["SESSION_BACKEND"] = "process"
os.environ["SECRET_KEY"] = "test-secret-key-not-used-in-production-0123456789"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import models  # noqa: E402
from database import SessionLocal, engine  # noqa: E402
from routers import proxy  # noqa: E402
from services import jupyter_manager, ratelimit, session_backend  # noqa: E402

FAKE_PID = 999_001
ADMIN_PW = "admin123"
# Every password created through the API must satisfy the platform policy
# (>= 10 chars, a letter and a digit).
ALICE_PW = "alice-pass1"


@pytest.fixture(scope="module")
def client():
    models.Base.metadata.create_all(bind=engine)

    # Force the process backend and monkeypatch the process manager so tests
    # never spawn real Jupyter servers or Docker containers.
    session_backend._backend = lambda: "process"
    jupyter_manager.start_jupyter = lambda *a, **kw: FAKE_PID
    jupyter_manager.is_process_alive = lambda pid: pid == FAKE_PID
    jupyter_manager.stop_jupyter = lambda pid: True
    jupyter_manager.find_available_port = lambda: 8177
    jupyter_manager.generate_token = lambda: "testtoken" * 8

    # Never actually proxy anywhere.
    proxy._find_session = lambda username: None

    # Seed admin.
    from auth import get_password_hash

    db = SessionLocal()
    if not db.query(models.User).filter(models.User.username == "admin").first():
        db.add(models.User(
            username="admin",
            email="admin@localhost",
            hashed_password=get_password_hash(ADMIN_PW),
            is_admin=True,
        ))
        db.commit()
    db.close()

    with TestClient(main.app) as c:
        yield c


@pytest.fixture(autouse=True)
def _clear_rate_limiter():
    """Login throttling is process-global; keep tests independent of order."""
    ratelimit._failures.clear()
    ratelimit._locked_until.clear()
    yield


@pytest.fixture(autouse=True)
def _clear_gpu_caches():
    """The GPU monitor caches its last reading, so tests must not inherit one.

    A test that fakes nvidia-smi leaves a fake GPU behind; the next test to
    call the real thing then finds a reading it cannot refresh and reports the
    telemetry as broken, which is true of nothing except the leftover.
    """
    yield
    from services import gpu_monitor

    gpu_monitor._UUID_CACHE.update({"at": 0.0, "by_uuid": {}, "by_index": {}})
    gpu_monitor._MINOR_CACHE.update({"at": 0.0, "by_index": {}})
    gpu_monitor._TELEMETRY.update({"at": 0.0, "gpus": [], "failing_since": None,
                                   "error": None})


def _login(client, username, password):
    res = client.post("/api/auth/login", data={"username": username, "password": password})
    assert res.status_code == 200, res.text
    return res.json()["access_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Health + auth
# ---------------------------------------------------------------------------

def test_health(client):
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_login_wrong_password(client):
    res = client.post("/api/auth/login", data={"username": "admin", "password": "nope"})
    assert res.status_code == 401


def test_me_requires_token(client):
    assert client.get("/api/user/me").status_code == 401


def test_login_sets_scoped_proxy_cookie(client):
    """Browser navigation to /jupyter/* has no Authorization header; the cookie
    is what lets the proxy authenticate it."""
    res = client.post("/api/auth/login", data={"username": "admin", "password": ADMIN_PW})
    cookie = res.headers.get("set-cookie", "")
    assert proxy.PROXY_COOKIE in cookie
    assert "HttpOnly" in cookie
    assert "Path=/jupyter" in cookie


def test_login_throttling_locks_out_after_repeated_failures(client):
    for _ in range(int(os.environ.get("LOGIN_MAX_FAILURES", 8))):
        client.post("/api/auth/login", data={"username": "admin", "password": "wrong"})
    res = client.post("/api/auth/login", data={"username": "admin", "password": ADMIN_PW})
    assert res.status_code == 429
    assert "Retry-After" in res.headers


# ---------------------------------------------------------------------------
# Admin workflow: create user → assign GPU → verify isolation
# ---------------------------------------------------------------------------

def test_full_admin_workflow(client):
    token = _login(client, "admin", ADMIN_PW)

    # Password policy is enforced at creation
    res = client.post(
        "/api/admin/users",
        headers=_auth(token),
        json={"username": "weak", "email": "weak@local", "password": "short1"},
    )
    assert res.status_code == 400

    # Create a normal user
    res = client.post(
        "/api/admin/users",
        headers=_auth(token),
        json={"username": "alice", "email": "alice@local", "password": ALICE_PW},
    )
    assert res.status_code == 201, res.text
    alice_id = res.json()["id"]

    # Duplicate username rejected
    res = client.post(
        "/api/admin/users",
        headers=_auth(token),
        json={"username": "alice", "email": "alice2@local", "password": ALICE_PW},
    )
    assert res.status_code == 409

    # Assign GPU 1 to alice
    res = client.post(
        "/api/admin/gpu/assignments",
        headers=_auth(token),
        json={"user_id": alice_id, "gpu_indices": [1]},
    )
    assert res.status_code == 201, res.text
    assert res.json()["gpu_indices"] == [1]

    # Out-of-range GPU rejected
    res = client.post(
        "/api/admin/gpu/assignments",
        headers=_auth(token),
        json={"user_id": alice_id, "gpu_indices": [99]},
    )
    assert res.status_code == 400

    # Regression: EVERY index is validated, not just the largest one.
    res = client.put(
        "/api/admin/gpu/assignments/1",
        headers=_auth(token),
        json={"gpu_indices": [0, 42, 1]},
    )
    assert res.status_code == 400
    assert "42" in res.json()["detail"]

    # Second assignment for same user rejected
    res = client.post(
        "/api/admin/gpu/assignments",
        headers=_auth(token),
        json={"user_id": alice_id, "gpu_indices": [0]},
    )
    assert res.status_code == 409

    # Regular user cannot access admin endpoints
    alice_token = _login(client, "alice", ALICE_PW)
    res = client.get("/api/admin/users", headers=_auth(alice_token))
    assert res.status_code == 403

    # Alice sees only her assigned GPU (mock monitor returns GPUs 0 and 1)
    res = client.get("/api/user/me/gpu", headers=_auth(alice_token))
    assert res.status_code == 200
    assert [g["index"] for g in res.json()] == [1]

    # …and the shared GPU endpoint is scoped for her too (it used to return
    # every GPU, including other users' process lists).
    res = client.get("/api/gpu/status", headers=_auth(alice_token))
    assert [g["index"] for g in res.json()] == [1]

    # Alice sees only herself via /me, with assignment embedded
    res = client.get("/api/user/me", headers=_auth(alice_token))
    assert res.status_code == 200
    body = res.json()
    assert body["username"] == "alice"
    assert body["gpu_assignment"]["gpu_indices"] == [1]


# ---------------------------------------------------------------------------
# Jupyter session lifecycle (monkeypatched process manager)
# ---------------------------------------------------------------------------

def test_jupyter_lifecycle(client):
    token = _login(client, "admin", ADMIN_PW)

    # No session yet → 404
    res = client.get("/api/user/me/jupyter/status", headers=_auth(token))
    assert res.status_code == 404

    # Start
    res = client.post("/api/user/me/jupyter/start", headers=_auth(token), json={})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "running"
    assert body["pid"] == FAKE_PID
    assert body["base_url"] == "/jupyter/admin/"
    # The owner gets the real token back even though it is encrypted at rest.
    assert body["token"] == "testtoken" * 8

    # Status reports running
    res = client.get("/api/user/me/jupyter/status", headers=_auth(token))
    assert res.json()["status"] == "running"

    # Stop
    res = client.post("/api/user/me/jupyter/stop", headers=_auth(token), json={})
    assert res.status_code == 200
    assert res.json()["status"] == "stopped"


def test_session_secrets_are_encrypted_at_rest(client):
    """A leaked database must not hand over live session credentials."""
    token = _login(client, "admin", ADMIN_PW)
    client.post("/api/user/me/jupyter/start", headers=_auth(token), json={})

    db = SessionLocal()
    try:
        row = db.query(models.JupyterSession).first()
        assert row.token.startswith("enc:v1:")
        assert "testtoken" not in row.token
    finally:
        db.close()

    client.post("/api/user/me/jupyter/stop", headers=_auth(token), json={})


def test_usage_record_opens_and_closes(client):
    """Every session lifetime lands in the accounting ledger."""
    token = _login(client, "admin", ADMIN_PW)
    client.post("/api/user/me/jupyter/start", headers=_auth(token), json={})

    res = client.get("/api/admin/usage", headers=_auth(token))
    assert res.status_code == 200
    open_rows = [s for s in res.json()["sessions"] if s["ended_at"] is None]
    assert open_rows, "starting a session must open a usage record"

    client.post("/api/user/me/jupyter/stop", headers=_auth(token), json={})
    res = client.get("/api/admin/usage", headers=_auth(token))
    closed = [s for s in res.json()["sessions"] if s["ended_at"]]
    assert closed and closed[0]["end_reason"] == "user"


def test_admin_sessions_and_force_stop(client):
    token = _login(client, "admin", ADMIN_PW)

    client.post("/api/user/me/jupyter/start", headers=_auth(token), json={})

    res = client.get("/api/admin/jupyter/sessions", headers=_auth(token))
    assert res.status_code == 200
    sessions = res.json()
    assert len(sessions) >= 1
    assert "user" in sessions[0]
    # Session secrets never leak through the admin API, not even to an admin.
    assert sessions[0]["token"] == "***"
    assert sessions[0]["ssh_password"] == "***"

    admin_user_id = sessions[0]["user_id"]
    res = client.post(f"/api/admin/jupyter/sessions/{admin_user_id}/stop", headers=_auth(token))
    assert res.status_code == 200


# ---------------------------------------------------------------------------
# Quotas
# ---------------------------------------------------------------------------

def test_disk_quota_costs_the_gpu_not_the_workspace(client, monkeypatch):
    """Over budget must not lock a user out of their own files.

    This used to assert a 429 on start.  That was the bug: the refusal told
    the user to delete files and simultaneously denied them the only place
    they could delete files from.  Being over disk now costs the GPU and the
    job queue instead.
    """
    token = _login(client, "admin", ADMIN_PW)
    res = client.get("/api/admin/users", headers=_auth(token))
    alice_id = next(u["id"] for u in res.json() if u["username"] == "alice")

    res = client.put(
        f"/api/admin/users/{alice_id}", headers=_auth(token),
        json={"disk_quota_mb": 10},
    )
    assert res.status_code == 200

    from services import metrics

    # Both seams: the bulk scan the dashboard reads, and the single-directory
    # re-measure the quota gate makes before it refuses anyone.
    monkeypatch.setattr(metrics, "user_disk_usage", lambda force=False: {"alice": 999})
    monkeypatch.setattr(metrics, "directory_size_mb",
                        lambda path, timeout=None, max_age=None: 999)

    alice_token = _login(client, "alice", ALICE_PW)
    res = client.post("/api/user/me/jupyter/start", headers=_auth(alice_token), json={})
    assert res.status_code == 200, res.text
    notice = res.json()["notice"]
    # The message names the numbers that matter and nothing about the
    # infrastructure underneath.
    assert "GPU" in notice
    assert not any(word in notice.lower()
                   for word in ("container", "cgroup", "docker", "host"))

    # A job, which only adds output, is still refused, with its own message.
    from services import quota as quota_service

    db = SessionLocal()
    try:
        alice = db.query(models.User).filter(models.User.id == alice_id).first()
        with pytest.raises(Exception):
            quota_service.assert_can_submit_job(db, alice)
    finally:
        db.close()

    # Lift the quota again so later tests are unaffected.
    client.put(f"/api/admin/users/{alice_id}", headers=_auth(token), json={"disk_quota_mb": 0})
    client.post("/api/user/me/jupyter/stop", headers=_auth(alice_token), json={})


def test_freeing_space_counts_before_the_scan_expires(client, monkeypatch):
    """Deleting files has to let a job through now, not in five minutes.

    Scanning every workspace is expensive, so the figure is cached, and the
    cached figure was what refused the job.  A user over budget therefore
    deleted files, submitted again and was told the same thing, for as long as
    the cache held, while `limits` inside their workspace measured live and
    said they were fine.  Being over disk is the one quota a user can answer,
    so the answer has to register.
    """
    from services import metrics, quota as quota_service

    token = _login(client, "admin", ADMIN_PW)
    res = client.get("/api/admin/users", headers=_auth(token))
    alice_id = next(u["id"] for u in res.json() if u["username"] == "alice")
    client.put(f"/api/admin/users/{alice_id}", headers=_auth(token),
               json={"disk_quota_mb": 100})

    def measure(path, timeout=None, max_age=None):
        # What the last scan recorded, against what a fresh `du` would find
        # now that the user has deleted something.
        return 50 if max_age is not None else 5000

    monkeypatch.setattr(metrics, "user_disk_usage", lambda force=False: {"alice": 5000})
    monkeypatch.setattr(metrics, "directory_size_mb", measure)

    db = SessionLocal()
    try:
        alice = db.query(models.User).filter(models.User.id == alice_id).first()
        state = quota_service.snapshot(db, alice)
        assert state["disk"]["used_mb"] == 50, "the stale figure was quoted back"
        assert state["disk"]["over"] is False
        # This is the case that matters: the job goes through without waiting
        # for the TTL.
        quota_service.assert_can_submit_job(db, alice)
    finally:
        db.close()

    client.put(f"/api/admin/users/{alice_id}", headers=_auth(token),
               json={"disk_quota_mb": 0})


def test_a_user_still_over_budget_is_refused(client, monkeypatch):
    """The re-measure must confirm the refusal, not soften it."""
    from services import metrics, quota as quota_service

    token = _login(client, "admin", ADMIN_PW)
    res = client.get("/api/admin/users", headers=_auth(token))
    alice_id = next(u["id"] for u in res.json() if u["username"] == "alice")
    client.put(f"/api/admin/users/{alice_id}", headers=_auth(token),
               json={"disk_quota_mb": 100})

    monkeypatch.setattr(metrics, "user_disk_usage", lambda force=False: {"alice": 5000})
    monkeypatch.setattr(metrics, "directory_size_mb",
                        lambda path, timeout=None, max_age=None: 4000)

    db = SessionLocal()
    try:
        alice = db.query(models.User).filter(models.User.id == alice_id).first()
        state = quota_service.snapshot(db, alice)
        assert state["disk"]["over"] is True
        # The number shown is the fresh one, so it matches what the user sees
        # in their own workspace rather than the scan they have outgrown.
        assert state["disk"]["used_mb"] == 4000
        with pytest.raises(Exception):
            quota_service.assert_can_submit_job(db, alice)
    finally:
        db.close()

    client.put(f"/api/admin/users/{alice_id}", headers=_auth(token),
               json={"disk_quota_mb": 0})


def test_resources_endpoint_reports_quota(client):
    alice_token = _login(client, "alice", ALICE_PW)
    res = client.get("/api/user/me/resources", headers=_auth(alice_token))
    assert res.status_code == 200
    body = res.json()
    assert "quota" in body and "disk" in body["quota"] and "gpu_hours" in body["quota"]


# ---------------------------------------------------------------------------
# Password management & token revocation
# ---------------------------------------------------------------------------

def test_password_reset_and_change(client):
    admin_token = _login(client, "admin", ADMIN_PW)

    res = client.get("/api/admin/users", headers=_auth(admin_token))
    alice_id = next(u["id"] for u in res.json() if u["username"] == "alice")

    res = client.post(
        f"/api/admin/users/{alice_id}/reset-password",
        headers=_auth(admin_token),
        json={"new_password": "newpass99-x"},
    )
    assert res.status_code == 200

    # Old password no longer works, new one does
    assert client.post(
        "/api/auth/login", data={"username": "alice", "password": ALICE_PW}
    ).status_code == 401
    alice_token = _login(client, "alice", "newpass99-x")

    # Self-service change with wrong old password
    res = client.put(
        "/api/auth/password",
        headers=_auth(alice_token),
        json={"old_password": "wrong", "new_password": "whatever12345"},
    )
    assert res.status_code == 400

    # Correct change, and it revokes the token that performed it
    res = client.put(
        "/api/auth/password",
        headers=_auth(alice_token),
        json={"old_password": "newpass99-x", "new_password": "finalpass123"},
    )
    assert res.status_code == 200
    assert client.get("/api/user/me", headers=_auth(alice_token)).status_code == 401
    assert _login(client, "alice", "finalpass123")


def test_deactivation_revokes_live_tokens(client):
    """A deactivated user used to keep working until their JWT expired."""
    admin_token = _login(client, "admin", ADMIN_PW)
    res = client.get("/api/admin/users", headers=_auth(admin_token))
    alice_id = next(u["id"] for u in res.json() if u["username"] == "alice")

    alice_token = _login(client, "alice", "finalpass123")
    assert client.get("/api/user/me", headers=_auth(alice_token)).status_code == 200

    client.put(f"/api/admin/users/{alice_id}", headers=_auth(admin_token),
               json={"is_active": False})
    assert client.get("/api/user/me", headers=_auth(alice_token)).status_code == 401

    client.put(f"/api/admin/users/{alice_id}", headers=_auth(admin_token),
               json={"is_active": True})


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------

def test_audit_trail_records_privileged_actions(client):
    token = _login(client, "admin", ADMIN_PW)
    res = client.get("/api/admin/audit", headers=_auth(token))
    assert res.status_code == 200
    actions = {row["action"] for row in res.json()}
    assert {"auth.login", "user.create", "gpu.assign"} <= actions
    assert "auth.login_failed" in actions


# ---------------------------------------------------------------------------
# Protected routes and proxy auth
# ---------------------------------------------------------------------------

def test_proxy_returns_404_without_session(client):
    token = _login(client, "admin", ADMIN_PW)
    res = client.get("/jupyter/admin/", headers=_auth(token), follow_redirects=False)
    # _find_session monkeypatched to None → user has no live server entry
    assert res.status_code == 404


def test_proxy_command_redaction():
    """Command lines shown in the GPU monitor must not leak session tokens."""
    from services.gpu_monitor import redact_command

    masked = redact_command("python jupyter-lab --ServerApp.token=abcdef123 --no-browser")
    assert "abcdef123" not in masked
    assert "--ServerApp.token=***" in masked
    # Ordinary arguments survive untouched
    assert redact_command("python train.py --lr 0.01") == "python train.py --lr 0.01"


def test_jupyter_password_hash_is_in_jupyter_format(client):
    """Jupyter dispatches on the ``argon2:`` prefix; without it passwd_check()
    takes the legacy branch and rejects even the correct password."""
    from auth import hash_jupyter_password, normalize_jupyter_hash

    digest = hash_jupyter_password("some-password-1")
    assert digest.startswith("argon2:$argon2")
    # Idempotent, and it repairs a bare hash written by an older version.
    assert normalize_jupyter_hash(digest) == digest
    bare = digest[len("argon2:"):]
    assert normalize_jupyter_hash(bare) == digest
    assert normalize_jupyter_hash(None) is None


def test_account_password_derives_jupyter_and_unix_credentials(client):
    """One password: the account password is what Jupyter and sshd accept."""
    token = _login(client, "admin", ADMIN_PW)
    res = client.post("/api/admin/users", headers=_auth(token), json={
        "username": "unified", "email": "unified@local", "password": "Unified-pass-1",
    })
    assert res.status_code == 201, res.text

    db = SessionLocal()
    try:
        user = db.query(models.User).filter(models.User.username == "unified").first()
        # Jupyter's format, and /etc/shadow's format, neither reversible.
        assert user.account_jupyter_hash.startswith("argon2:$argon2")
        assert user.unix_password_hash.startswith("$6$")
        from passlib.hash import sha512_crypt

        assert sha512_crypt.verify("Unified-pass-1", user.unix_password_hash)
        assert not sha512_crypt.verify("wrong-password", user.unix_password_hash)
        # The plaintext is nowhere to be found.
        assert "Unified-pass-1" not in (user.hashed_password + user.unix_password_hash
                                        + user.account_jupyter_hash)
    finally:
        db.close()


def test_login_backfills_derived_credentials(client):
    """Accounts predating unified passwords are repaired on next login."""
    db = SessionLocal()
    try:
        user = db.query(models.User).filter(models.User.username == "unified").first()
        user.account_jupyter_hash = None
        user.unix_password_hash = None
        db.add(user)
        db.commit()
    finally:
        db.close()

    _login(client, "unified", "Unified-pass-1")

    db = SessionLocal()
    try:
        user = db.query(models.User).filter(models.User.username == "unified").first()
        assert user.account_jupyter_hash and user.unix_password_hash
    finally:
        db.close()


def test_changing_the_password_refreshes_derived_credentials(client):
    token = _login(client, "unified", "Unified-pass-1")
    db = SessionLocal()
    try:
        before = db.query(models.User).filter(
            models.User.username == "unified").first().unix_password_hash
    finally:
        db.close()

    res = client.put("/api/auth/password", headers=_auth(token),
                     json={"old_password": "Unified-pass-1", "new_password": "Unified-pass-2"})
    assert res.status_code == 200

    from passlib.hash import sha512_crypt

    db = SessionLocal()
    try:
        after = db.query(models.User).filter(
            models.User.username == "unified").first().unix_password_hash
        assert after != before
        assert sha512_crypt.verify("Unified-pass-2", after)
        assert not sha512_crypt.verify("Unified-pass-1", after)
    finally:
        db.close()


def test_cpu_cores_is_separate_from_the_cpu_seconds_budget(client):
    """Regression: the assignment form's only CPU field used to be
    `cpu_limit_seconds`, and the container backend turned it into cores with
    `seconds / 3600` floored at 0.25, so an admin asking for 4 cores got 0.25."""
    token = _login(client, "admin", ADMIN_PW)
    res = client.post("/api/admin/users", headers=_auth(token), json={
        "username": "cputest", "email": "cputest@local", "password": "Cputest-pass-1",
    })
    user_id = res.json()["id"]

    res = client.post("/api/admin/gpu/assignments", headers=_auth(token), json={
        "user_id": user_id, "gpu_indices": [0], "cpu_cores": 4, "memory_limit_mb": 2048,
    })
    assert res.status_code == 201, res.text
    assert res.json()["cpu_cores"] == 4
    assert res.json()["cpu_limit_seconds"] is None

    # And the resolver hands the container backend cores, not seconds.
    from routers.user import _resolve_limits

    db = SessionLocal()
    try:
        user = db.query(models.User).filter(models.User.username == "cputest").first()
        memory_mb, cores, seconds, procs = _resolve_limits(user)
        assert memory_mb == 2048
        assert cores == 4
        assert seconds in (None, 0)
        assert procs == 512     # platform default when the admin set none
    finally:
        db.close()


def test_mis_entered_cpu_seconds_are_rescued_as_cores():
    """A value no larger than the host core count cannot be a CPU-seconds
    budget, so the migration re-reads it as the core count it meant."""
    import os

    from init_db import _rescue_cpu_core_values

    db = SessionLocal()
    try:
        user = db.query(models.User).filter(models.User.username == "cputest").first()
        assignment = user.gpu_assignment
        assignment.cpu_cores = None
        assignment.cpu_limit_seconds = 4          # what an admin typed for "4 cores"
        db.add(assignment)
        # A genuine budget, far above the core count: must be left alone.
        assignment_id = assignment.id
        db.commit()
    finally:
        db.close()

    _rescue_cpu_core_values()

    db = SessionLocal()
    try:
        assignment = db.query(models.GpuAssignment).filter(
            models.GpuAssignment.id == assignment_id).first()
        assert assignment.cpu_cores == 4
        assert assignment.cpu_limit_seconds is None

        assignment.cpu_cores = None
        assignment.cpu_limit_seconds = (os.cpu_count() or 8) * 1000
        db.add(assignment)
        db.commit()
    finally:
        db.close()

    _rescue_cpu_core_values()

    db = SessionLocal()
    try:
        assignment = db.query(models.GpuAssignment).filter(
            models.GpuAssignment.id == assignment_id).first()
        assert assignment.cpu_cores is None, "a real CPU-seconds budget must survive"
        assert assignment.cpu_limit_seconds == (os.cpu_count() or 8) * 1000
    finally:
        db.close()


def test_fair_share_orders_by_allocation_not_arrival(client):
    """Ten jobs from one user must not push a later submitter behind them.

    This is what the queue exists for.  FIFO would serve whoever asked for
    the most, first.
    """
    from datetime import datetime, timedelta

    from auth import get_password_hash
    from services import fairshare

    db = SessionLocal()
    try:
        heavy = models.User(username="heavy", email="heavy@local",
                            hashed_password=get_password_hash("x"))
        light = models.User(username="light", email="light@local",
                            hashed_password=get_password_hash("x"))
        db.add_all([heavy, light])
        db.commit()
        db.refresh(heavy); db.refresh(light)

        # The heavy user has been running all morning; the light one has not.
        db.add(models.UsageRecord(
            user_id=heavy.id, username="heavy", gpu_count=1, cpu_cores=4,
            backend="job", started_at=datetime.utcnow() - timedelta(minutes=30),
            ended_at=datetime.utcnow() - timedelta(minutes=1),
            gpu_seconds=1800, cpu_seconds=7200,
        ))
        db.commit()

        base = datetime.utcnow()
        for i in range(10):
            db.add(models.Job(user_id=heavy.id, username="heavy", script="a.sh",
                              workdir="", gpu_count=1, gpu_memory_mb=1000,
                              created_at=base + timedelta(seconds=i)))
        db.commit()
        # Submitted last, by someone who has used nothing.
        late = models.Job(user_id=light.id, username="light", script="b.sh",
                          workdir="", gpu_count=1, gpu_memory_mb=1000,
                          created_at=base + timedelta(seconds=99))
        db.add(late)
        db.commit()
        db.refresh(late)

        scores = fairshare.scores(db)
        assert scores[heavy.id] > scores[light.id], "recent usage must raise the score"

        from services import jobs as job_service

        order = job_service.queued_order(db)
        assert order[late.id] == 1, (
            "the user who has used nothing should be served first, "
            f"but got position {order[late.id]}"
        )

        # And within one user, order is still by submission time.
        heavy_jobs = sorted(
            [j for j in db.query(models.Job).filter(models.Job.username == "heavy").all()],
            key=lambda j: order[j.id],
        )
        assert [j.created_at for j in heavy_jobs] == sorted(j.created_at for j in heavy_jobs)
    finally:
        db.close()


def test_usage_records_cpu_time_for_gpu_less_work(client):
    """A CPU-only job consumes real capacity and must appear in the statistics."""
    from auth import get_password_hash
    from services import fairshare

    db = SessionLocal()
    try:
        user = db.query(models.User).filter(models.User.username == "cpuonly").first()
        if user is None:
            user = models.User(username="cpuonly", email="cpuonly@local",
                               hashed_password=get_password_hash("x"))
            db.add(user)
            db.commit()
            db.refresh(user)
        db.add(models.UsageRecord(
            user_id=user.id, username="cpuonly", gpu_count=0, cpu_cores=8,
            backend="job", gpu_seconds=0, cpu_seconds=3600,
        ))
        db.commit()
        user_id = user.id

        scores = fairshare.scores(db)
        assert scores[user_id] > 0, "CPU-only work must still count toward fair share"
    finally:
        db.close()

    token = _login(client, "admin", ADMIN_PW)
    res = client.get("/api/admin/usage", headers=_auth(token))
    assert res.status_code == 200
    totals = {row["username"]: row for row in res.json()["totals"]}
    # An hour of eight cores, with no GPU at all, still shows up.
    assert totals["cpuonly"]["cpu_hours"] >= 1.0
    assert totals["cpuonly"]["gpu_hours"] == 0
    assert "fair_share" in res.json()


def test_fair_share_counts_a_stretch_longer_than_the_window(client):
    """A job older than the window still owes for the hours inside it.

    Selecting usage rows by ``started_at`` dropped any stretch that began before
    the window, so a job running longer than ``FAIRSHARE_WINDOW_HOURS`` made its
    owner look idle and sent them to the *front* of the queue -- and the moment
    it ended its whole ledger row was already out of range.  The window is a
    window on time, not on start times: only the part outside it is forgiven.
    """
    from datetime import datetime, timedelta

    from auth import get_password_hash
    from config import settings
    from services import fairshare

    window = settings.FAIRSHARE_WINDOW_HOURS   # 24 by default
    db = SessionLocal()
    try:
        marathon = models.User(username="marathon", email="marathon@local",
                               hashed_password=get_password_hash("x"))
        sprinter = models.User(username="sprinter", email="sprinter@local",
                               hashed_password=get_password_hash("x"))
        db.add_all([marathon, sprinter])
        db.commit()
        db.refresh(marathon); db.refresh(sprinter)

        now = datetime.utcnow()

        # One card held without a break since before the window opened, and
        # still running: nothing is booked on the row yet.
        db.add(models.UsageRecord(
            user_id=marathon.id, username="marathon", gpu_count=1, cpu_cores=0,
            backend="job", started_at=now - timedelta(hours=window + 1),
            ended_at=None,
        ))
        # A one-hour job by somebody else, finished a moment ago.
        db.add(models.UsageRecord(
            user_id=sprinter.id, username="sprinter", gpu_count=1, cpu_cores=0,
            backend="job", started_at=now - timedelta(hours=1),
            ended_at=now - timedelta(seconds=1), gpu_seconds=3600,
        ))
        db.commit()
        marathon_id, sprinter_id = marathon.id, sprinter.id

        history = fairshare.historical_usage(db, now=now)

        # Charged for the hours inside the window, not for the whole stretch and
        # not for nothing.
        assert history[marathon_id] == pytest.approx(window * 3600, rel=0.01), (
            "a stretch that began before the window must still be charged for "
            "the part of it inside the window"
        )
        assert history[marathon_id] > history[sprinter_id], (
            "holding a card for a whole day must not score below a one-hour job"
        )

        # The same row, now closed: still the in-window hours, minus the part
        # that has since fallen out of range.
        row = (
            db.query(models.UsageRecord)
            .filter(models.UsageRecord.user_id == marathon_id)
            .one()
        )
        row.ended_at = now
        row.gpu_seconds = (window + 1) * 3600
        db.commit()

        closed = fairshare.historical_usage(db, now=now)
        assert closed[marathon_id] == pytest.approx(window * 3600, rel=0.01), (
            "closing the row must not change what the window sees"
        )

        # And a stretch that ended before the window opened is gone for good.
        row.started_at = now - timedelta(hours=window + 3)
        row.ended_at = now - timedelta(hours=window + 1)
        db.commit()
        assert marathon_id not in fairshare.historical_usage(db, now=now), (
            "usage that ended before the window must not count at all"
        )
    finally:
        db.close()


def test_fair_share_charges_a_running_batch_job_before_it_ends(client):
    """A job holding a card must be felt while it runs, not only afterwards.

    Only an interactive workspace opens a ledger row at start (``usage.open_record``);
    a batch job gets its row from ``jobs._book_segment`` when it leaves the
    machine.  Reading usage from the ledger alone therefore made a running job
    weigh nothing in the decayed sum however long it had held the hardware, and
    then delivered the whole stretch at once once it finished.
    """
    from datetime import datetime, timedelta

    from auth import get_password_hash
    from config import settings
    from services import fairshare

    window = settings.FAIRSHARE_WINDOW_HOURS
    db = SessionLocal()
    added = []
    try:
        holder = models.User(username="cardholder", email="cardholder@local",
                             hashed_password=get_password_hash("x"))
        db.add(holder)
        db.commit()
        db.refresh(holder)

        now = datetime.utcnow()
        # Running longer than the window, and with no usage record of its own,
        # which is what a real batch job looks like mid-flight.
        job = models.Job(
            user_id=holder.id, username="cardholder", script="long.sh", workdir="",
            gpu_count=1, gpu_memory_mb=1000, cpu_cores=0,
            status=models.JobStatus.running,
            started_at=now - timedelta(hours=window + 1),
        )
        db.add(job)
        db.commit()
        added = [job, holder]
        holder_id = holder.id

        assert (
            db.query(models.UsageRecord)
            .filter(models.UsageRecord.user_id == holder_id)
            .count() == 0
        ), "precondition: a running job has no ledger row yet"

        history = fairshare.historical_usage(db, now=now)
        assert history[holder_id] == pytest.approx(window * 3600, rel=0.01), (
            "a job still running must be charged for the hours it has held the "
            "card inside the window, not only once it ends"
        )

        # The lookahead deposit is a separate term and is still added on top.
        assert fairshare.current_allocation(db)[holder_id] == pytest.approx(
            settings.FAIRSHARE_LOOKAHEAD_SECONDS, rel=0.01
        )
    finally:
        for row in added:
            db.delete(row)
        db.commit()
        db.close()


def test_ssh_port_belongs_to_the_workspace_it_was_given_to(monkeypatch):
    """A port is assigned once and stays with its workspace.

    An SSH client keys ``known_hosts`` by host *and* port, so moving a workspace
    to another port makes every client that has ever connected announce that the
    host key has changed -- indistinguishable, from the user's seat, from a
    machine-in-the-middle, and not clearable by reconnecting.  "Lowest free port
    wins" handed a stopped workspace's port to whoever started next, which is
    exactly how two users end up trading fingerprints.
    """
    from services import container_manager as cm

    start = cm.settings.SSH_PORT_START
    monkeypatch.setattr(cm, "published_host_ports", lambda: set())

    # The assigned port comes first, even though it is not the lowest free one.
    assert cm.ssh_port_candidates(preferred=start + 7)[0] == start + 7

    # A port assigned to somebody else is never offered, whether or not their
    # workspace is running: that is what makes stopping one safe.
    order = cm.ssh_port_candidates(reserved=[start, start + 1])
    assert start not in order and start + 1 not in order
    assert order[0] == start + 2

    # A workspace with no port yet takes the lowest one left.
    assert cm.ssh_port_candidates()[0] == start

    # An assigned port that something outside the platform has taken must not
    # stop the workspace coming up; it starts elsewhere and records that.
    monkeypatch.setattr(cm, "published_host_ports", lambda: {start + 7})
    order = cm.ssh_port_candidates(preferred=start + 7)
    assert start + 7 not in order and order[0] == start


def test_deactivating_a_user_returns_their_ssh_port(client):
    """The pool takes a port back only when the account can no longer use it.

    Held forever it would be a port nobody can reach; released on every stop it
    would be a port somebody else gets, which is the fingerprint clash this all
    exists to prevent.  Deactivation is the line between the two.
    """
    from auth import get_password_hash

    token = _login(client, "admin", ADMIN_PW)
    db = SessionLocal()
    try:
        user = models.User(username="porthold", email="porthold@local",
                           hashed_password=get_password_hash("x"))
        db.add(user)
        db.commit()
        db.refresh(user)
        db.add(models.JupyterSession(
            user_id=user.id, port=9911, token="x",
            base_url=f"/jupyter/{user.username}/",
            ssh_port=2299, status=models.SessionStatus.stopped,
        ))
        db.commit()
        user_id = user.id
    finally:
        db.close()

    res = client.put(f"/api/admin/users/{user_id}", headers=_auth(token),
                     json={"is_active": False})
    assert res.status_code == 200

    db = SessionLocal()
    try:
        session = (
            db.query(models.JupyterSession)
            .filter(models.JupyterSession.user_id == user_id)
            .one()
        )
        assert session.ssh_port is None, (
            "a deactivated account must give its SSH port back to the pool"
        )
    finally:
        db.close()


def test_finished_job_runtime_is_not_counted_twice():
    """A finished job must report the time it ran, not twice the time it ran.

    ``runtime_seconds`` holds what ``_book_segment`` has already charged, and
    ``_close`` charges the final stretch and then leaves ``started_at`` alone as
    the record of when the job last started.  Adding the span from
    ``started_at`` on top of that doubled every completed job: a 30-minute run
    was served to the dashboard as 3,612 seconds and rendered as "1h 0m".
    """
    from datetime import datetime, timedelta

    from services import jobs as job_service

    start = datetime(2026, 9, 30, 7, 18, 45)
    half_hour = 1806.0

    finished = models.Job(
        user_id=1, username="x", script="s.sh", workdir="",
        status=models.JobStatus.succeeded,
        started_at=start, finished_at=start + timedelta(seconds=half_hour),
        runtime_seconds=half_hour,
    )
    assert job_service._elapsed_seconds(finished, finished.finished_at) == 1806

    # Still running: nothing is booked yet, so the live stretch is all there is.
    running = models.Job(
        user_id=1, username="x", script="s.sh", workdir="",
        status=models.JobStatus.running, started_at=start, runtime_seconds=0.0,
    )
    assert job_service._elapsed_seconds(
        running, start + timedelta(seconds=half_hour)) == 1806

    # Resumed after a pause: booked stretches plus the live one.
    resumed = models.Job(
        user_id=1, username="x", script="s.sh", workdir="",
        status=models.JobStatus.running, started_at=start,
        runtime_seconds=600.0,
    )
    assert job_service._elapsed_seconds(
        resumed, start + timedelta(seconds=300)) == 900

    # Frozen: the clock is stopped, and started_at was cleared when it froze.
    paused = models.Job(
        user_id=1, username="x", script="s.sh", workdir="",
        status=models.JobStatus.paused, started_at=None, runtime_seconds=900.0,
    )
    assert job_service._elapsed_seconds(paused) == 900


def test_home_mapping_is_validated_not_guessed(client, tmp_path):
    """A mapping must be named by an administrator and must stay inside the
    configured root.  Inferring it from the username would hand a host account
    to whoever registered that name on the platform."""
    from fastapi import HTTPException

    from config import settings
    from services import workspaces

    root = tmp_path / "homes"
    (root / "alice").mkdir(parents=True)
    # A real home belongs to a real person; the validation refuses root-owned
    # directories, so the fixture has to look like one.
    os.chown(root / "alice", 1001, 1001)
    settings.HOME_MOUNT_ROOT = str(root)

    assert workspaces.validate_home_path(str(root / "alice")) == str(root / "alice")
    assert workspaces.validate_home_path("") == ""

    for bad, why in (
        ("relative/path", "not absolute"),
        ("/etc", "outside the root"),
        (str(root), "the root itself"),
        (str(root / "nope"), "does not exist"),
    ):
        try:
            workspaces.validate_home_path(bad)
        except HTTPException:
            pass
        else:
            raise AssertionError(f"{bad!r} should have been refused ({why})")


def test_mapped_workspace_keeps_its_own_owner(client, tmp_path, monkeypatch):
    """The container account adapts to the files, not the other way round:
    chowning somebody's home to the platform's uid would take their own files
    away from them outside the container."""
    from config import settings
    from services import container_manager, workspaces

    root = tmp_path / "homes"
    home = root / "bob"
    home.mkdir(parents=True)
    os.chown(home, 1001, 1001)
    settings.HOME_MOUNT_ROOT = str(root)

    db = SessionLocal()
    try:
        from auth import get_password_hash

        user = models.User(username="bob", email="bob@local",
                           hashed_password=get_password_hash("x"),
                           home_path=str(home))
        db.add(user)
        db.commit()
        db.refresh(user)

        space = workspaces.for_user(user)
        assert space.mapped is True
        assert space.host_path == str(home)
        # Whatever uid owns the directory is the uid the account gets.
        assert space.uid == home.stat().st_uid
        assert space.gid == home.stat().st_gid

        # And the ownership repair refuses to touch it.
        called = []
        monkeypatch.setattr(container_manager.subprocess, "run",
                            lambda *a, **k: called.append(a) or (_ for _ in ()).throw(
                                AssertionError("chown must not run on a mapped home")))
        container_manager.ensure_workspace_ownership("bob", space)
        assert called == [], "a mapped home must never be re-owned"

        # An unmapped user still gets the platform-owned workspace.
        plain = models.User(username="carol", email="carol@local",
                            hashed_password=get_password_hash("x"))
        db.add(plain)
        db.commit()
        db.refresh(plain)
        assert workspaces.for_user(plain).mapped is False
    finally:
        db.close()


def test_logout(client):
    token = _login(client, "admin", ADMIN_PW)
    res = client.post("/api/auth/logout", headers=_auth(token))
    assert res.status_code == 200


# ---------------------------------------------------------------------------
# Trash: deleting a user is reversible, and never touches a real home
# ---------------------------------------------------------------------------

TRASH_PW = "trash-pass1"


def _make_user(client, token, username, **extra):
    res = client.post("/api/admin/users", headers=_auth(token), json={
        "username": username, "email": f"{username}@local", "password": TRASH_PW, **extra,
    })
    assert res.status_code == 201, res.text
    return res.json()["id"]


def test_delete_moves_the_user_to_the_trash(client):
    token = _login(client, "admin", ADMIN_PW)
    user_id = _make_user(client, token, "trashme")

    assert client.delete(f"/api/admin/users/{user_id}", headers=_auth(token)).status_code == 200

    # Gone from the live list, present in the trash.
    listed = {u["username"] for u in client.get("/api/admin/users", headers=_auth(token)).json()}
    assert "trashme" not in listed
    trash = client.get("/api/admin/trash", headers=_auth(token)).json()
    assert [u["username"] for u in trash["users"]] == ["trashme"]

    # They cannot sign in, and the name cannot be taken by someone else.
    res = client.post("/api/auth/login", data={"username": "trashme", "password": TRASH_PW})
    assert res.status_code == 403

    res = client.post("/api/admin/users", headers=_auth(token), json={
        "username": "trashme", "email": "someone@else", "password": TRASH_PW,
    })
    assert res.status_code == 409
    assert "trash" in res.json()["detail"].lower()


def test_restore_brings_the_user_and_their_files_back(client, tmp_path):
    from services import trash

    token = _login(client, "admin", ADMIN_PW)
    user_id = _make_user(client, token, "comeback")

    # Give them a workspace with something in it.
    root = os.environ["JUPYTER_DATA_DIR"]
    workspace = os.path.join(root, "comeback")
    os.makedirs(workspace, exist_ok=True)
    with open(os.path.join(workspace, "thesis.txt"), "w") as handle:
        handle.write("months of work")

    client.delete(f"/api/admin/users/{user_id}", headers=_auth(token))

    # The directory was renamed, not removed.
    assert not os.path.exists(workspace)
    archives = [n for n in os.listdir(root) if n.startswith(trash.ARCHIVE_PREFIX)]
    assert any(n.startswith(".deleted-comeback-") for n in archives)

    res = client.post(f"/api/admin/trash/{user_id}/restore", headers=_auth(token))
    assert res.status_code == 200, res.text

    with open(os.path.join(workspace, "thesis.txt")) as handle:
        assert handle.read() == "months of work"
    assert _login(client, "comeback", TRASH_PW)


def test_purge_deletes_the_archive_and_frees_the_name(client):
    token = _login(client, "admin", ADMIN_PW)
    user_id = _make_user(client, token, "goodbye")
    root = os.environ["JUPYTER_DATA_DIR"]
    os.makedirs(os.path.join(root, "goodbye"), exist_ok=True)

    client.delete(f"/api/admin/users/{user_id}", headers=_auth(token))
    archive = client.get("/api/admin/trash", headers=_auth(token)).json()["users"]
    archive = next(u for u in archive if u["username"] == "goodbye")["archived_workspace"]
    assert os.path.isdir(os.path.join(root, archive))

    assert client.delete(f"/api/admin/trash/{user_id}", headers=_auth(token)).status_code == 200
    assert not os.path.exists(os.path.join(root, archive))

    # The name is available again only now.
    assert _make_user(client, token, "goodbye")


def test_a_mapped_home_is_never_renamed_or_deleted(client, tmp_path):
    """Mapping exists for this: the files belong to a person, not to us."""
    from services import trash

    home = tmp_path / "realhome"
    home.mkdir()
    (home / "precious.txt").write_text("irreplaceable")

    token = _login(client, "admin", ADMIN_PW)
    user_id = _make_user(client, token, "mapped")
    # Set the mapping directly: the API only accepts paths under
    # HOME_MOUNT_ROOT, which a pytest tmp_path is not.
    db = SessionLocal()
    try:
        user = db.query(models.User).filter(models.User.id == user_id).first()
        user.home_path = str(home)
        db.commit()
    finally:
        db.close()

    client.delete(f"/api/admin/users/{user_id}", headers=_auth(token))
    assert (home / "precious.txt").read_text() == "irreplaceable"

    client.delete(f"/api/admin/trash/{user_id}", headers=_auth(token))
    assert home.is_dir(), "a purge must not remove somebody's home directory"
    assert (home / "precious.txt").read_text() == "irreplaceable"


def test_directory_purge_refuses_anything_outside_the_data_root(client):
    from services import trash

    for name in ("..", "../etc", "/etc", ".", "a/b", ""):
        assert trash.remove_directory(name) is False


def test_the_trash_lists_directories_no_account_owns(client):
    token = _login(client, "admin", ADMIN_PW)
    root = os.environ["JUPYTER_DATA_DIR"]
    os.makedirs(os.path.join(root, "ghost"), exist_ok=True)

    orphans = client.get("/api/admin/trash", headers=_auth(token)).json()["orphans"]
    assert "ghost" in {o["name"] for o in orphans}

    # A live user's workspace is not an orphan and cannot be deleted this way.
    _make_user(client, token, "livinguser")
    os.makedirs(os.path.join(root, "livinguser"), exist_ok=True)
    orphans = client.get("/api/admin/trash", headers=_auth(token)).json()["orphans"]
    assert "livinguser" not in {o["name"] for o in orphans}
    res = client.delete("/api/admin/trash/directories/livinguser", headers=_auth(token))
    assert res.status_code == 400

    assert client.delete("/api/admin/trash/directories/ghost", headers=_auth(token)).status_code == 200
    assert not os.path.exists(os.path.join(root, "ghost"))


# ---------------------------------------------------------------------------
# Assignments without a GPU
# ---------------------------------------------------------------------------

def test_an_assignment_can_cap_cpu_and_ram_with_no_gpu(client):
    """Limiting someone who gets no GPU is an ordinary thing to want."""
    token = _login(client, "admin", ADMIN_PW)
    user_id = _make_user(client, token, "cpuonlyuser")

    res = client.post("/api/admin/gpu/assignments", headers=_auth(token), json={
        "user_id": user_id, "memory_limit_mb": 4096, "cpu_cores": 2,
    })
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["gpu_indices"] == []
    assert body["memory_limit_mb"] == 4096
    assert body["cpu_cores"] == 2

    # And the user's session starts with no GPU rather than failing.
    listed = client.get("/api/admin/gpu/assignments", headers=_auth(token)).json()
    assert any(a["id"] == body["id"] and a["gpu_indices"] == [] for a in listed)


def test_an_assignment_that_sets_nothing_is_refused(client):
    token = _login(client, "admin", ADMIN_PW)
    user_id = _make_user(client, token, "emptyassign")

    res = client.post("/api/admin/gpu/assignments", headers=_auth(token),
                      json={"user_id": user_id})
    assert res.status_code == 400
    assert "at least one" in res.json()["detail"]

    res = client.post("/api/admin/gpu/assignments", headers=_auth(token),
                      json={"user_id": user_id, "gpu_indices": [], "memory_limit_mb": None})
    assert res.status_code == 400


def test_clearing_a_limit_on_an_existing_assignment_removes_it(client):
    """A field blanked in the form arrives as null and must take effect.

    Keying on ``is not None`` meant a limit could be raised but never removed.
    """
    token = _login(client, "admin", ADMIN_PW)
    user_id = _make_user(client, token, "clearlimits")

    created = client.post("/api/admin/gpu/assignments", headers=_auth(token), json={
        "user_id": user_id, "gpu_indices": [0], "memory_limit_mb": 4096, "cpu_cores": 2,
    }).json()

    res = client.put(f"/api/admin/gpu/assignments/{created['id']}", headers=_auth(token),
                     json={"gpu_indices": [0], "memory_limit_mb": None, "cpu_cores": None,
                           "cpu_limit_seconds": None})
    assert res.status_code == 200, res.text
    assert res.json()["memory_limit_mb"] is None
    assert res.json()["cpu_cores"] is None
    assert res.json()["gpu_indices"] == [0]

    # Dropping the GPU too leaves nothing at all, which is refused.
    res = client.put(f"/api/admin/gpu/assignments/{created['id']}", headers=_auth(token),
                     json={"gpu_indices": [], "memory_limit_mb": None, "cpu_cores": None,
                           "cpu_limit_seconds": None})
    assert res.status_code == 400

    # Giving up the GPU while keeping a RAM cap is fine.
    res = client.put(f"/api/admin/gpu/assignments/{created['id']}", headers=_auth(token),
                     json={"gpu_indices": [], "memory_limit_mb": 2048})
    assert res.status_code == 200, res.text
    assert res.json()["gpu_indices"] == []
    assert res.json()["memory_limit_mb"] == 2048


# ---------------------------------------------------------------------------
# Per-user process ceiling
# ---------------------------------------------------------------------------

def test_an_admin_can_raise_the_process_limit_for_one_user(client):
    """512 is a platform default, not a verdict: a parallel build needs more."""
    from routers.user import _resolve_limits

    token = _login(client, "admin", ADMIN_PW)
    user_id = _make_user(client, token, "manyprocs")

    res = client.post("/api/admin/gpu/assignments", headers=_auth(token), json={
        "user_id": user_id, "cpu_cores": 8, "max_processes": 4096,
    })
    assert res.status_code == 201, res.text
    assert res.json()["max_processes"] == 4096

    db = SessionLocal()
    try:
        user = db.query(models.User).filter(models.User.id == user_id).first()
        assert _resolve_limits(user)[3] == 4096
    finally:
        db.close()


def test_the_process_limit_alone_is_a_valid_assignment(client):
    token = _login(client, "admin", ADMIN_PW)
    user_id = _make_user(client, token, "procsonly")

    res = client.post("/api/admin/gpu/assignments", headers=_auth(token), json={
        "user_id": user_id, "max_processes": 1024,
    })
    assert res.status_code == 201, res.text
    assert res.json()["gpu_indices"] == []
    assert res.json()["max_processes"] == 1024


def test_a_process_limit_too_low_to_start_a_workspace_is_refused(client):
    """A ceiling below what a session needs looks like a broken platform."""
    token = _login(client, "admin", ADMIN_PW)
    user_id = _make_user(client, token, "tinyprocs")

    res = client.post("/api/admin/gpu/assignments", headers=_auth(token), json={
        "user_id": user_id, "max_processes": 4,
    })
    assert res.status_code == 400
    assert "at least 16" in res.json()["detail"]


def test_the_process_limit_can_be_cleared_back_to_the_default(client):
    token = _login(client, "admin", ADMIN_PW)
    user_id = _make_user(client, token, "resetprocs")
    created = client.post("/api/admin/gpu/assignments", headers=_auth(token), json={
        "user_id": user_id, "cpu_cores": 2, "max_processes": 2048,
    }).json()

    res = client.put(f"/api/admin/gpu/assignments/{created['id']}", headers=_auth(token),
                     json={"cpu_cores": 2, "max_processes": None})
    assert res.status_code == 200, res.text
    assert res.json()["max_processes"] is None


# ---------------------------------------------------------------------------
# Being over the disk budget
# ---------------------------------------------------------------------------

def _set_disk_usage(monkeypatch, mb):
    """Pretend every workspace is this big, without writing that much."""
    from services import quota as quota_service

    monkeypatch.setattr(quota_service, "disk_used_mb",
                        lambda username, user=None, max_age=None: mb)


def test_over_disk_the_workspace_still_starts(client, monkeypatch):
    """Refusing the start left the user no way to delete anything.

    The error said "delete some files" while denying them the only place they
    could delete files from.
    """
    token = _login(client, "admin", ADMIN_PW)
    user_id = _make_user(client, token, "fullup")
    client.put(f"/api/admin/users/{user_id}", headers=_auth(token),
               json={"disk_quota_mb": 100})

    _set_disk_usage(monkeypatch, 500)
    user_token = _login(client, "fullup", TRASH_PW)

    res = client.get("/api/user/me/resources", headers=_auth(user_token))
    assert res.json()["quota"]["disk"]["over"] is True

    res = client.post("/api/user/me/jupyter/start", headers=_auth(user_token), json={})
    assert res.status_code == 200, res.text
    assert "over" in res.json()["notice"].lower()


def test_over_disk_costs_the_gpu_and_the_job_queue(client, monkeypatch):
    token = _login(client, "admin", ADMIN_PW)
    user_id = _make_user(client, token, "nogpuwhenfull")
    client.put(f"/api/admin/users/{user_id}", headers=_auth(token),
               json={"disk_quota_mb": 100})
    client.post("/api/admin/gpu/assignments", headers=_auth(token),
                json={"user_id": user_id, "gpu_indices": [0]})

    _set_disk_usage(monkeypatch, 500)
    user_token = _login(client, "nogpuwhenfull", TRASH_PW)
    client.post("/api/user/me/jupyter/start", headers=_auth(user_token), json={})

    # The usage ledger records what the session actually got: no GPU.
    db = SessionLocal()
    try:
        row = (
            db.query(models.UsageRecord)
            .filter(models.UsageRecord.user_id == user_id,
                    models.UsageRecord.ended_at.is_(None))
            .first()
        )
        assert row is not None
        assert (row.gpu_indices or "") == "", "a workspace over budget must get no GPU"
    finally:
        db.close()

    # And a job, which only adds more output, is refused.
    from services import quota as quota_service

    db = SessionLocal()
    try:
        user = db.query(models.User).filter(models.User.id == user_id).first()
        with pytest.raises(Exception) as caught:
            quota_service.assert_can_submit_job(db, user)
        assert "429" in str(caught.value) or "Free some" in str(caught.value)
    finally:
        db.close()


def test_the_admin_view_measures_a_mapped_home(client, monkeypatch, tmp_path):
    """"0 MB" in red: the number and the verdict came from different sources.

    The row read disk usage from the cached scan of JUPYTER_DATA_DIR, which
    knows nothing about a mapped home, while the quota beside it was measured
    at the workspace's real location.
    """
    home = tmp_path / "bighome"
    home.mkdir()

    token = _login(client, "admin", ADMIN_PW)
    user_id = _make_user(client, token, "mappedbig")
    client.put(f"/api/admin/users/{user_id}", headers=_auth(token),
               json={"disk_quota_mb": 100})
    db = SessionLocal()
    try:
        user = db.query(models.User).filter(models.User.id == user_id).first()
        user.home_path = str(home)
        db.commit()
    finally:
        db.close()

    from services import metrics

    monkeypatch.setattr(metrics, "directory_size_mb",
                        lambda path, timeout=None, max_age=None: 500)

    row = next(
        u for u in client.get("/api/admin/resources", headers=_auth(token)).json()["users"]
        if u["username"] == "mappedbig"
    )
    assert row["quota"]["disk"]["over"] is True
    assert row["disk_used_mb"] == 500, "the figure must match the verdict beside it"


def test_a_quota_stop_leaves_a_grace_period(client, monkeypatch):
    """Otherwise "stop" lands within a minute of every start, forever."""
    from datetime import datetime, timedelta

    from config import settings
    from services import quota as quota_service, reaper, session_backend

    token = _login(client, "admin", ADMIN_PW)
    user_id = _make_user(client, token, "graceuser")
    client.put(f"/api/admin/users/{user_id}", headers=_auth(token),
               json={"disk_quota_mb": 100})

    monkeypatch.setattr(quota_service, "disk_used_mb",
                        lambda username, user=None, max_age=None: 500)
    monkeypatch.setattr(settings, "DISK_QUOTA_ACTION", "stop")
    stopped = []
    monkeypatch.setattr(session_backend, "stop_session",
                        lambda *a, **kw: stopped.append(a) or True)

    user_token = _login(client, "graceuser", TRASH_PW)
    client.post("/api/user/me/jupyter/start", headers=_auth(user_token), json={})

    # Just started: reported, but left alone so they can delete something.
    offenders = reaper.enforce_disk_quota()
    assert any(o["user"] == "graceuser" for o in offenders)
    assert stopped == [], "a session must not be stopped before it can be used"

    # Once the grace period has passed, the configured action applies.
    db = SessionLocal()
    try:
        session = (
            db.query(models.JupyterSession)
            .filter(models.JupyterSession.user_id == user_id)
            .first()
        )
        session.created_at = datetime.utcnow() - timedelta(
            minutes=settings.DISK_QUOTA_GRACE_MINUTES + 1
        )
        db.commit()
    finally:
        db.close()

    reaper.enforce_disk_quota()
    assert stopped, "after the grace period the quota is enforced"


# ---------------------------------------------------------------------------
# Where a workspace appears inside the container
# ---------------------------------------------------------------------------

def test_a_workspace_is_mounted_at_a_home_shaped_path(client):
    """``/workspace`` is not a home, and things installed into a home say so.

    conda writes its absolute prefix into .bashrc, into every wrapper script's
    shebang and into conda-meta; pip does the same for ``--user`` installs.
    Mounting somebody's real home somewhere else left all of them pointing at
    a path that no longer existed.
    """
    from services import workspaces

    db = SessionLocal()
    try:
        from auth import get_password_hash

        plain = models.User(username="HomeShaped", email="hs@local",
                            hashed_password=get_password_hash("x"))
        db.add(plain)
        db.commit()
        db.refresh(plain)
        # Lowercased and sanitised, exactly like the container account name.
        assert workspaces.for_user(plain).container_path == "/home/homeshaped"
    finally:
        db.close()


def test_a_mapped_home_keeps_its_own_absolute_path(client, tmp_path):
    """/home/trongchi/miniconda3 has to still be that exact path."""
    from services import workspaces

    home = tmp_path / "trongchi"
    home.mkdir()

    db = SessionLocal()
    try:
        from auth import get_password_hash

        user = models.User(username="mappedpath", email="mp@local",
                           hashed_password=get_password_hash("x"),
                           home_path=str(home))
        db.add(user)
        db.commit()
        db.refresh(user)

        space = workspaces.for_user(user)
        assert space.mapped is True
        assert space.container_path == str(home), (
            "a mapped home must appear inside at the same path it has outside"
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Job queue: CPU capacity is measured, not assumed
# ---------------------------------------------------------------------------

def _measured(monkeypatch, *, cpu_percent, cpu_count=32, age=0.0):
    """Pretend the metrics collector last saw this much load."""
    import time as _time

    from services import metrics

    monkeypatch.setattr(metrics, "latest", lambda: {
        "collected_at": _time.time() - age,
        "host": {"cpu_count": cpu_count, "cpu_percent": cpu_percent},
        "containers": [],
    })


def test_idle_cores_are_offered_even_when_every_ceiling_is_allocated(monkeypatch):
    """A core count is a ceiling, not a reservation.

    Three idle workspaces allocated 34 cores between them, and adding those up
    left a 32-core machine reporting nothing free.  Jobs then queued forever
    behind CPU that was sitting there unused.
    """
    from config import settings as cfg
    from services import jobs as job_service

    monkeypatch.setattr(cfg, "JOB_CPU_POOL_CORES", 28)
    _measured(monkeypatch, cpu_percent=12.5)          # 12.5% of 32 = 4 cores
    monkeypatch.setattr(job_service, "_allocated_cores", lambda db: 34.0)
    monkeypatch.setattr(job_service, "_unramped_cores", lambda db: 0.0)

    cap = job_service.cpu_capacity(None)
    assert cap["basis"] == "measured"
    assert cap["committed_cores"] == 4.0, "the ceilings were counted again"
    assert cap["free_cores"] == 24.0


def test_a_machine_that_really_is_busy_still_refuses(monkeypatch):
    """Measuring must not mean always saying yes."""
    from config import settings as cfg
    from services import jobs as job_service

    monkeypatch.setattr(cfg, "JOB_CPU_POOL_CORES", 28)
    _measured(monkeypatch, cpu_percent=95.0)          # 30.4 of 32 cores busy
    monkeypatch.setattr(job_service, "_unramped_cores", lambda db: 0.0)

    assert job_service.cpu_capacity(None)["free_cores"] == 0.0


def test_a_job_admitted_moments_ago_holds_its_cores(monkeypatch):
    """Otherwise the next pass admits a second job into the first one's idle.

    A job that is still importing torch shows no load, so without this the
    scheduler would keep admitting work into the same idle reading until the
    machine was swamped.
    """
    from config import settings as cfg
    from services import jobs as job_service

    monkeypatch.setattr(cfg, "JOB_CPU_POOL_CORES", 28)
    _measured(monkeypatch, cpu_percent=12.5)          # 4 cores busy
    monkeypatch.setattr(job_service, "_unramped_cores", lambda db: 20.0)

    cap = job_service.cpu_capacity(None)
    assert cap["committed_cores"] == 24.0
    assert cap["free_cores"] == 4.0, "a second 20-core job must not fit"


def test_an_unmeasured_machine_falls_back_to_the_ceilings(monkeypatch):
    """Right after a restart, or when docker stats is unavailable.

    Refusing work is the safe way to be wrong here; flooding the box is not.
    """
    from config import settings as cfg
    from services import jobs as job_service, metrics

    monkeypatch.setattr(cfg, "JOB_CPU_POOL_CORES", 28)
    monkeypatch.setattr(job_service, "_allocated_cores", lambda db: 34.0)

    monkeypatch.setattr(metrics, "latest", lambda: {})
    cap = job_service.cpu_capacity(None)
    assert cap["basis"] == "allocated"
    assert cap["free_cores"] == 0.0

    # A reading nobody has refreshed is no better than no reading at all.
    _measured(monkeypatch, cpu_percent=0.0, age=cfg.JOB_CPU_MEASURE_MAX_AGE_SECONDS + 30)
    assert job_service.cpu_capacity(None)["basis"] == "allocated"


# ---------------------------------------------------------------------------
# GPU processes: who owns them, and what an admin may stop
# ---------------------------------------------------------------------------

def test_only_a_mapped_uid_claims_a_host_process(client, tmp_path):
    """Attribution by uid is only honest for users tied to a real home.

    A user without a mapping owns their workspace at the container's own uid,
    which belongs to nobody on this machine.  Claiming host processes with it
    would file somebody else's training run under their name, and then offer
    an admin a button to stop it.
    """
    from services import gpu_monitor

    home = tmp_path / "hostmapped"
    home.mkdir()
    uid = home.stat().st_uid

    db = SessionLocal()
    try:
        from auth import get_password_hash

        db.add(models.User(username="hostmapped", email="hm@local",
                           hashed_password=get_password_hash("x"),
                           home_path=str(home)))
        db.add(models.User(username="nothostmapped", email="nhm@local",
                           hashed_password=get_password_hash("x")))
        db.commit()
    finally:
        db.close()

    gpu_monitor._UID_MAP_CACHE.update({"at": 0.0, "accounts": {"by_uid": {}, "names": set()}})
    mapping = gpu_monitor.platform_uid_map()
    assert mapping.get(uid) == "hostmapped"
    assert "nothostmapped" not in mapping.values(), (
        "a uid nobody mapped must not be claimed by name alone"
    )


def _gpu_process_stub(monkeypatch, *, live=None, owner="alice", uid=1003, mains=None):
    from services import gpu_monitor

    monkeypatch.setattr(gpu_monitor, "_compute_pids",
                        lambda: {4242: 8000} if live is None else live)
    monkeypatch.setattr(gpu_monitor, "_container_main_pids", lambda: mains or {})
    monkeypatch.setattr(gpu_monitor, "pid_owner_map", lambda: {})
    monkeypatch.setattr(gpu_monitor, "_process_owner", lambda pid, owners: owner)
    monkeypatch.setattr(gpu_monitor, "process_uid", lambda pid: uid)
    monkeypatch.setattr(gpu_monitor, "_get_process_name", lambda pid, known=None: "python train.py")
    sent = []
    monkeypatch.setattr(gpu_monitor.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    return sent


def test_stopping_a_gpu_process_signals_it(monkeypatch):
    from services import gpu_monitor

    sent = _gpu_process_stub(monkeypatch)
    result = gpu_monitor.stop_compute_process(4242)

    assert sent == [(4242, gpu_monitor.signal.SIGTERM)]
    assert result["user"] == "alice"
    assert result["memory_freed_mb"] == 8000


def test_a_pid_that_is_no_longer_computing_is_refused(monkeypatch):
    """The screen it came from is seconds old and PIDs get reused.

    Stopping the wrong process is worse than refusing to stop one.
    """
    from services import gpu_monitor

    sent = _gpu_process_stub(monkeypatch, live={})
    with pytest.raises(Exception):
        gpu_monitor.stop_compute_process(4242)
    assert sent == [], "nothing may be signalled on a stale pid"


def test_a_process_the_platform_cannot_name_is_refused(monkeypatch):
    """Another stack's work on the same card is not the platform's to stop."""
    from services import gpu_monitor

    sent = _gpu_process_stub(monkeypatch, owner=None)
    with pytest.raises(Exception):
        gpu_monitor.stop_compute_process(4242)
    assert sent == []


def test_a_root_process_is_refused(monkeypatch):
    from services import gpu_monitor

    sent = _gpu_process_stub(monkeypatch, uid=0)
    with pytest.raises(Exception):
        gpu_monitor.stop_compute_process(4242)
    assert sent == []


def test_a_containers_own_pid_is_refused(monkeypatch):
    """Killing PID 1 would end the session with no record that it ended."""
    from services import gpu_monitor

    sent = _gpu_process_stub(monkeypatch, mains={4242: "gpu-jupyter-alice"})
    with pytest.raises(Exception):
        gpu_monitor.stop_compute_process(4242)
    assert sent == []


def test_a_host_account_that_is_a_platform_account_is_that_user(monkeypatch):
    """Working over SSH does not put you outside the platform.

    Their run was filed under nobody, and the panel offered an admin no way to
    deal with it, purely because nobody had mapped their home directory.
    """
    from services import gpu_monitor

    monkeypatch.setattr(gpu_monitor, "process_uid", lambda pid: 4321)
    monkeypatch.setattr(gpu_monitor, "platform_uid_map", lambda: {})
    monkeypatch.setattr(gpu_monitor, "_os_username", lambda uid: "trongchi")
    monkeypatch.setattr(gpu_monitor, "platform_usernames", lambda: {"trongchi", "uet"})

    assert gpu_monitor._process_owner(999, {}) == "trongchi"


def test_a_host_account_the_platform_does_not_know_stays_outside(monkeypatch):
    """uid 1000 is `ubuntu` here, and `ubuntu` is nobody's account.

    Claiming it would put system work under a user's name and then offer a
    button to kill it.
    """
    from services import gpu_monitor

    monkeypatch.setattr(gpu_monitor, "process_uid", lambda pid: 1000)
    monkeypatch.setattr(gpu_monitor, "platform_uid_map", lambda: {})
    monkeypatch.setattr(gpu_monitor, "_os_username", lambda uid: "ubuntu")
    monkeypatch.setattr(gpu_monitor, "platform_usernames", lambda: {"trongchi", "uet"})

    assert gpu_monitor._process_owner(999, {}) is None


def test_a_batch_jobs_processes_are_attributed_too(monkeypatch):
    """A job holds a card exactly as a workspace does.

    Asking only for role=jupyter left every job container unattributed, so a
    user's own job looked like somebody else's work on their card.
    """
    from services import container_manager, gpu_monitor

    asked = {}

    def fake_list(role="jupyter"):
        asked["role"] = role
        return []

    monkeypatch.setattr(container_manager, "list_platform_containers", fake_list)
    gpu_monitor._OWNER_CACHE.update({"at": 0.0, "map": {}})
    gpu_monitor.pid_owner_map()

    assert asked["role"] is None, "job containers were filtered out of attribution"


def _fake_nvidia_smi(monkeypatch, uuid="GPU-test-0"):
    from services import gpu_monitor

    def run(cmd, timeout=10):
        joined = " ".join(cmd)
        if "--query-compute-apps" in joined:
            return f"{uuid}, 4242, 8000\n"
        if "index,uuid," in joined:
            return f"0, {uuid}, Test GPU, 24576, 8000, 16576, 40, 10, 55, 100\n"
        if "index,uuid" in joined:
            return f"0, {uuid}\n"
        return ""

    monkeypatch.setattr(gpu_monitor, "_run", run)
    monkeypatch.setattr(gpu_monitor, "platform_gpu_usage", lambda: {})
    monkeypatch.setattr(gpu_monitor, "pid_owner_map", lambda: {})
    monkeypatch.setattr(gpu_monitor, "_process_owner", lambda pid, owners: "someone-else")
    monkeypatch.setattr(gpu_monitor, "process_uid", lambda pid: 1003)
    monkeypatch.setattr(gpu_monitor, "_get_process_name",
                        lambda pid, known=None: "python /home/someone-else/secret/train.py")
    gpu_monitor._UUID_CACHE.update({"at": 0.0, "by_uuid": {}, "by_index": {}})


def test_a_users_view_of_a_shared_gpu_carries_no_command_lines(monkeypatch):
    """Two users can be assigned the same card.

    Filtering by index does not keep them apart, so the line each of them sees
    about the other must not name their files or their project.
    """
    from services import gpu_monitor

    _fake_nvidia_smi(monkeypatch)
    proc = gpu_monitor.get_gpu_status()[0]["processes"][0]

    assert proc["pid"] == 4242
    assert proc["user"] == "someone-else"
    assert proc["memory_used_mb"] == 8000
    assert "name" not in proc, "a command line reached a user's view"
    assert "stoppable" not in proc


def test_an_admin_asks_for_the_command_line_explicitly(monkeypatch):
    from services import gpu_monitor

    _fake_nvidia_smi(monkeypatch)
    proc = gpu_monitor.get_gpu_status(detailed=True)[0]["processes"][0]

    assert proc["name"].startswith("python /home/someone-else")
    assert proc["stoppable"] is True


def test_reading_the_accounts_twice_uses_the_cache_without_crashing():
    """The second read is the one that goes through the cache.

    Every other test resets the cache first, so nothing exercised the warm
    path, and a stale key left in it took the entire GPU view down the moment
    two requests arrived within the cache's lifetime.
    """
    from services import gpu_monitor

    gpu_monitor._UID_MAP_CACHE.update(
        {"at": 0.0, "accounts": {"by_uid": {}, "names": set()}})

    cold_uids = gpu_monitor.platform_uid_map()
    cold_names = gpu_monitor.platform_usernames()
    warm_uids = gpu_monitor.platform_uid_map()
    warm_names = gpu_monitor.platform_usernames()

    assert warm_uids == cold_uids
    assert warm_names == cold_names
    assert isinstance(warm_names, set)


# ---------------------------------------------------------------------------
# Self-service: profile, password
# ---------------------------------------------------------------------------

def _new_account(client, username, password, email=None):
    """Create an account through the admin API and sign in as it."""
    admin_token = _login(client, "admin", ADMIN_PW)
    res = client.post(
        "/api/admin/users",
        headers=_auth(admin_token),
        json={
            "username": username,
            "email": email or f"{username}@local",
            "password": password,
            "full_name": username.title(),
        },
    )
    assert res.status_code == 201, res.text
    return res.json()["id"], _login(client, username, password)


def test_a_user_edits_their_own_name_and_email(client):
    """Correcting your own name used to need an administrator."""
    _, token = _new_account(client, "selfedit", "Selfedit-pass1")

    res = client.put(
        "/api/user/me/profile",
        headers=_auth(token),
        json={"full_name": "  Self Edit  ", "email": "self.edit@example.org"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["full_name"] == "Self Edit", "surrounding space must be trimmed"
    assert res.json()["email"] == "self.edit@example.org"

    # And it is persisted, not just echoed back.
    again = client.get("/api/user/me", headers=_auth(token))
    assert again.json()["email"] == "self.edit@example.org"


def test_a_user_cannot_take_an_email_that_is_already_in_use(client):
    _new_account(client, "emailowner", "Emailowner-pass1", email="taken@example.org")
    _, token = _new_account(client, "emailclash", "Emailclash-pass1")

    res = client.put(
        "/api/user/me/profile",
        headers=_auth(token),
        json={"email": "taken@example.org"},
    )
    assert res.status_code == 409
    # Whose address it is stays private.
    assert "emailowner" not in res.json()["detail"].lower()


def test_the_profile_form_rejects_an_address_that_is_not_one(client):
    _, token = _new_account(client, "bademail", "Bademail-pass1")

    res = client.put(
        "/api/user/me/profile", headers=_auth(token), json={"email": "not-an-email"},
    )
    assert res.status_code == 400
    assert client.get("/api/user/me", headers=_auth(token)).json()["email"] == "bademail@local"


def test_the_profile_form_cannot_grant_privileges(client):
    """It takes name and email.  Anything else sent with them is not applied."""
    _, token = _new_account(client, "climber", "Climber-pass1")

    res = client.put(
        "/api/user/me/profile",
        headers=_auth(token),
        json={
            "full_name": "Climber",
            "is_admin": True,
            "disk_quota_mb": 10_000_000,
            "gpu_hours_quota": 9999,
            "home_path": "/etc",
        },
    )
    assert res.status_code == 200
    me = client.get("/api/user/me", headers=_auth(token)).json()
    assert me["is_admin"] is False
    assert me["disk_quota_mb"] is None
    assert me["gpu_hours_quota"] is None
    assert me["home_path"] is None


def test_changing_your_password_does_not_end_the_session_doing_it(client):
    """The old behaviour threw the user out of the dashboard mid-run.

    Every token minted before the change still dies, and that is what makes a
    stolen session expire, but the browser that just proved it knows both
    passwords is handed a replacement.
    """
    _, token = _new_account(client, "staysignedin", "Staysignedin-pass1")

    res = client.put(
        "/api/auth/password",
        headers=_auth(token),
        json={"old_password": "Staysignedin-pass1", "new_password": "Staysignedin-pass2"},
    )
    assert res.status_code == 200, res.text

    fresh = res.json()["access_token"]
    assert fresh and fresh != token
    assert client.get("/api/user/me", headers=_auth(fresh)).status_code == 200

    # The token it replaced is dead, and so is every other browser's.
    assert client.get("/api/user/me", headers=_auth(token)).status_code == 401


def test_changing_your_password_moves_the_ssh_and_jupyter_credentials_with_it(client):
    """One password covers the platform, SSH and Jupyter, so all three change."""
    from passlib.hash import sha512_crypt

    _, token = _new_account(client, "onepassword", "Onepassword-pass1")

    db = SessionLocal()
    try:
        before = db.query(models.User).filter(
            models.User.username == "onepassword").first().account_jupyter_hash
    finally:
        db.close()

    res = client.put(
        "/api/auth/password",
        headers=_auth(token),
        json={"old_password": "Onepassword-pass1", "new_password": "Onepassword-pass2"},
    )
    assert res.status_code == 200, res.text

    db = SessionLocal()
    try:
        user = db.query(models.User).filter(models.User.username == "onepassword").first()
        assert sha512_crypt.verify("Onepassword-pass2", user.unix_password_hash)
        assert not sha512_crypt.verify("Onepassword-pass1", user.unix_password_hash)
        assert user.account_jupyter_hash != before
        assert user.account_jupyter_hash.startswith("argon2:")
    finally:
        db.close()


def test_a_separate_jupyter_password_is_only_replaced_when_asked(client):
    """It was chosen as a second factor; changing the account password is not
    by itself a decision to give that up."""
    _, token = _new_account(client, "twofactor", "Twofactor-pass1")

    assert client.put(
        "/api/user/me/jupyter-password",
        headers=_auth(token),
        json={"password": "notebook-secret-1"},
    ).status_code == 200

    res = client.put(
        "/api/auth/password",
        headers=_auth(token),
        json={
            "old_password": "Twofactor-pass1",
            "new_password": "Twofactor-pass2",
            "reset_jupyter_password": False,
        },
    )
    assert res.status_code == 200
    assert res.json()["jupyter_password_reset"] is False
    token = res.json()["access_token"]
    assert client.get("/api/user/me", headers=_auth(token)).json()["jupyter_password_set"] is True

    # Asking for it, on the other hand, puts Jupyter back on the account password.
    res = client.put(
        "/api/auth/password",
        headers=_auth(token),
        json={
            "old_password": "Twofactor-pass2",
            "new_password": "Twofactor-pass3",
            "reset_jupyter_password": True,
        },
    )
    assert res.status_code == 200
    assert res.json()["jupyter_password_reset"] is True
    token = res.json()["access_token"]
    assert client.get("/api/user/me", headers=_auth(token)).json()["jupyter_password_set"] is False


def test_the_new_password_must_actually_be_new(client):
    _, token = _new_account(client, "samepass", "Samepass-pass1")

    res = client.put(
        "/api/auth/password",
        headers=_auth(token),
        json={"old_password": "Samepass-pass1", "new_password": "Samepass-pass1"},
    )
    assert res.status_code == 400
    # And the account still works with it.
    assert _login(client, "samepass", "Samepass-pass1")


# ---------------------------------------------------------------------------
# Jobs: paging
# ---------------------------------------------------------------------------

def test_jobs_arrive_one_page_at_a_time(client):
    """A user with a long history should not be sent all of it to draw ten rows."""
    user_id, token = _new_account(client, "manyjobs", "Manyjobs-pass1")

    db = SessionLocal()
    try:
        for i in range(25):
            db.add(models.Job(
                user_id=user_id, username="manyjobs", script=f"job{i}.sh",
                workdir="", gpu_count=0, gpu_memory_mb=0,
                status=models.JobStatus.succeeded,
            ))
        db.commit()
    finally:
        db.close()

    first = client.get("/api/jobs", headers=_auth(token),
                       params={"page": 1, "per_page": 10}).json()
    assert len(first["jobs"]) == 10
    assert first["pagination"] == {"page": 1, "per_page": 10, "total": 25, "pages": 3}

    last = client.get("/api/jobs", headers=_auth(token),
                      params={"page": 3, "per_page": 10}).json()
    assert len(last["jobs"]) == 5
    assert last["pagination"]["page"] == 3

    # Newest first, and no job appears on two pages.
    second = client.get("/api/jobs", headers=_auth(token),
                        params={"page": 2, "per_page": 10}).json()
    ids = [j["id"] for j in first["jobs"] + second["jobs"] + last["jobs"]]
    assert ids == sorted(ids, reverse=True)
    assert len(set(ids)) == 25

    # A page past the end lands on the last real one rather than going blank:
    # a poll must not empty the table when jobs are swept up under it.
    beyond = client.get("/api/jobs", headers=_auth(token),
                        params={"page": 99, "per_page": 10}).json()
    assert beyond["pagination"]["page"] == 3
    assert len(beyond["jobs"]) == 5


def test_the_active_count_is_not_just_this_page(client):
    """"2 active" must not read "0 active" because both are on page three."""
    user_id, token = _new_account(client, "activecount", "Activecount-pass1")

    db = SessionLocal()
    try:
        for i in range(12):
            db.add(models.Job(
                user_id=user_id, username="activecount", script=f"done{i}.sh",
                workdir="", gpu_count=0, status=models.JobStatus.succeeded,
            ))
        db.commit()
        for i in range(2):
            db.add(models.Job(
                user_id=user_id, username="activecount", script=f"waiting{i}.sh",
                workdir="", gpu_count=1, status=models.JobStatus.queued,
            ))
        db.commit()
    finally:
        db.close()

    page_two = client.get("/api/jobs", headers=_auth(token),
                          params={"page": 2, "per_page": 10}).json()
    assert page_two["active_total"] == 2
    assert not any(j["status"] == "queued" for j in page_two["jobs"]), (
        "the queued jobs are the newest, so they are on page one"
    )


def test_the_workspace_commands_still_get_an_unpaged_list(client):
    """`queue` prints one list in a terminal; it has no pages to turn."""
    user_id, token = _new_account(client, "cliview", "Cliview-pass1")

    db = SessionLocal()
    try:
        for i in range(15):
            db.add(models.Job(
                user_id=user_id, username="cliview", script=f"j{i}.sh",
                workdir="", gpu_count=0, status=models.JobStatus.succeeded,
            ))
        db.commit()
    finally:
        db.close()

    res = client.get("/api/jobs", headers=_auth(token)).json()
    assert len(res["jobs"]) == 15
    assert res["pagination"]["total"] == 15
    assert res["pagination"]["pages"] == 1


# ---------------------------------------------------------------------------
# Jobs: what a submission may ask for
# ---------------------------------------------------------------------------

from datetime import datetime, timedelta  # noqa: E402


def _submit(client, token, **payload):
    """POST a job the way the `submit` command does."""
    body = {"script": "job.sh", "workdir": ""}
    body.update(payload)
    return client.post("/api/jobs", headers=_auth(token), json=body)


def _job_user(client, username):
    """An account with a script in its workspace, ready to submit."""
    import pathlib

    user_id, token = _new_account(client, username, f"{username.title()}-pass1")
    root = pathlib.Path(os.environ["JUPYTER_DATA_DIR"]) / username
    root.mkdir(parents=True, exist_ok=True)
    (root / "job.sh").write_text("#!/bin/sh\necho hi\n")
    return user_id, token


def test_a_job_that_names_no_vram_is_a_cpu_job(client):
    """No flags at all is the CPU case, not a GPU job with a guessed figure.

    The platform used to fill in 4096 MB, so the commonest way to get a
    reservation wrong was to say nothing, and the scheduler would then fit a
    neighbour into space that was never free.
    """
    _, token = _job_user(client, "cpujob")

    res = _submit(client, token)
    assert res.status_code == 201, res.text
    assert res.json()["gpu_count"] == 0
    assert res.json()["gpu_memory_mb"] == 0


def test_asking_for_a_gpu_without_a_figure_is_refused(client):
    _, token = _job_user(client, "nofigure")

    res = _submit(client, token, gpu_count=2)
    assert res.status_code == 400
    assert "--gpu-memory" in res.json()["detail"]
    # The previous `submit` sends a GPU count on its own, so the wording must
    # not send that user hunting for a --gpus flag they never typed.
    assert "--gpus" not in res.json()["detail"]


def test_a_vram_figure_is_what_turns_a_job_into_a_gpu_job(client):
    _, token = _job_user(client, "gpujob")

    res = _submit(client, token, gpu_memory_mb=2000)
    assert res.status_code == 201, res.text
    assert res.json()["gpu_count"] == 1, "one GPU unless more were asked for"
    assert res.json()["gpu_memory_mb"] == 2000


def test_zero_vram_means_cpu_rather_than_an_error(client):
    _, token = _job_user(client, "zerovram")

    res = _submit(client, token, gpu_memory_mb=0)
    assert res.status_code == 201, res.text
    assert res.json()["gpu_count"] == 0


def test_no_gpu_and_a_vram_figure_contradict(client):
    _, token = _job_user(client, "contradict")

    res = _submit(client, token, gpu_count=0, gpu_memory_mb=8000)
    assert res.status_code == 400
    assert "contradict" in res.json()["detail"].lower()


def test_the_runtime_limit_is_accepted_bounded_and_visible(client):
    """--max-minutes was enforced but never shown back, so nobody could
    check what their job was actually held to."""
    _, token = _job_user(client, "timed")

    res = _submit(client, token, max_runtime_minutes=30)
    assert res.status_code == 201, res.text
    assert res.json()["max_runtime_minutes"] == 30

    # Above the platform ceiling it is refused, not silently trimmed.
    from config import settings

    original = settings.JOB_MAX_RUNTIME_MINUTES
    settings.JOB_MAX_RUNTIME_MINUTES = 60
    try:
        over = _submit(client, token, max_runtime_minutes=600)
        assert over.status_code == 400
        assert "60" in over.json()["detail"]
        # And the ceiling applies to a job that asked for nothing.
        default = _submit(client, token)
        assert default.json()["max_runtime_minutes"] == 60
    finally:
        settings.JOB_MAX_RUNTIME_MINUTES = original


# ---------------------------------------------------------------------------
# Jobs: the reservation is enforced
# ---------------------------------------------------------------------------

def test_the_vram_allowance_covers_the_cuda_context(client):
    """A job asking for exactly what its tensors need must not be stopped for
    the runtime's own overhead, which it never chose."""
    from config import settings
    from services import jobs as job_service

    factor, grace = settings.JOB_GPU_OVERRUN_FACTOR, settings.JOB_GPU_OVERRUN_GRACE_MB
    settings.JOB_GPU_OVERRUN_FACTOR = 1.1
    settings.JOB_GPU_OVERRUN_GRACE_MB = 1024
    try:
        # Small request: the flat grace is the larger allowance.
        assert job_service.gpu_overrun_allowance_mb(2000) == 3024
        # Large request: the proportional one takes over.
        assert job_service.gpu_overrun_allowance_mb(40000) == 44000
    finally:
        settings.JOB_GPU_OVERRUN_FACTOR = factor
        settings.JOB_GPU_OVERRUN_GRACE_MB = grace


def test_a_job_holding_more_vram_than_it_asked_for_is_stopped(client, monkeypatch):
    """Understating the reservation jumps the queue and kills the neighbour,
    so the figure is held to."""
    from services import container_manager, jobs as job_service

    user_id, _ = _job_user(client, "greedy")
    stopped, removed = [], []
    monkeypatch.setattr(container_manager, "stop_job_container",
                        lambda job_id, timeout=10: stopped.append(job_id) or True)
    monkeypatch.setattr(container_manager, "remove_job_container",
                        lambda job_id: removed.append(job_id))
    # 30 GB held against a 4 GB reservation, far past any allowance.
    monkeypatch.setattr(job_service, "job_gpu_memory_mb", lambda job: 30000)

    db = SessionLocal()
    try:
        job = models.Job(
            user_id=user_id, username="greedy", script="job.sh", workdir="",
            gpu_count=1, gpu_memory_mb=4096, gpu_indices="0",
            status=models.JobStatus.running, started_at=datetime.utcnow(),
        )
        db.add(job)
        db.commit()
        db.refresh(job)

        job_service._check_gpu_overrun(db, job)
        db.commit()
        db.refresh(job)

        assert job.status == models.JobStatus.failed
        assert stopped == [job.id], "the container must actually be stopped"
        assert removed == [job.id], "and removed, or it lingers exited forever"
        # The message has to carry the real figure, or the user cannot fix it.
        assert "30000" in job.message
        assert "--gpu-memory" in job.message
    finally:
        db.close()


def test_a_job_inside_its_allowance_is_left_alone(client, monkeypatch):
    from services import container_manager, jobs as job_service

    user_id, _ = _job_user(client, "honest")
    monkeypatch.setattr(container_manager, "stop_job_container",
                        lambda job_id, timeout=10: (_ for _ in ()).throw(
                            AssertionError("an honest job was stopped")))
    # 4.5 GB held against a 4 GB reservation: inside the CUDA-context grace.
    monkeypatch.setattr(job_service, "job_gpu_memory_mb", lambda job: 4500)

    db = SessionLocal()
    try:
        job = models.Job(
            user_id=user_id, username="honest", script="job.sh", workdir="",
            gpu_count=1, gpu_memory_mb=4096, gpu_indices="0",
            status=models.JobStatus.running, started_at=datetime.utcnow(),
        )
        db.add(job)
        db.commit()
        db.refresh(job)

        job_service._check_gpu_overrun(db, job)
        db.refresh(job)
        assert job.status == models.JobStatus.running
        assert not job.message
    finally:
        db.close()


def test_a_cpu_job_is_never_checked_for_vram(client, monkeypatch):
    """It holds no GPU, so there is nothing to measure and nothing to stop."""
    from services import jobs as job_service

    user_id, _ = _job_user(client, "vramskip")
    monkeypatch.setattr(job_service, "job_gpu_memory_mb",
                        lambda job: (_ for _ in ()).throw(
                            AssertionError("a CPU job was measured for VRAM")))

    db = SessionLocal()
    try:
        job = models.Job(
            user_id=user_id, username="vramskip", script="job.sh", workdir="",
            gpu_count=0, gpu_memory_mb=0,
            status=models.JobStatus.running, started_at=datetime.utcnow(),
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_service._check_gpu_overrun(db, job)   # must not raise
        db.refresh(job)
        assert job.status == models.JobStatus.running
    finally:
        db.close()


def test_a_job_submitted_before_the_rule_is_flagged_not_stopped(client, monkeypatch):
    """Its owner read a document saying the figure was advisory.

    A deployment that changes its mind is not an argument for killing a run
    that is already hours in, so those jobs are only flagged.
    """
    from services import container_manager, jobs as job_service

    user_id, _ = _job_user(client, "grandfathered")
    monkeypatch.setattr(container_manager, "stop_job_container",
                        lambda job_id, timeout=10: (_ for _ in ()).throw(
                            AssertionError("a pre-policy job was stopped")))
    monkeypatch.setattr(job_service, "job_gpu_memory_mb", lambda job: 43768)

    db = SessionLocal()
    try:
        job = models.Job(
            user_id=user_id, username="grandfathered", script="job.sh", workdir="",
            gpu_count=1, gpu_memory_mb=20000, gpu_indices="0",
            gpu_memory_enforced=False,
            status=models.JobStatus.running, started_at=datetime.utcnow(),
        )
        db.add(job)
        db.commit()
        db.refresh(job)

        job_service._check_gpu_overrun(db, job)
        db.commit()
        db.refresh(job)

        assert job.status == models.JobStatus.running, "it must keep running"
        # Still told, so the next submission carries an honest figure.
        assert "43768" in job.message
        assert "--gpu-memory" in job.message
    finally:
        db.close()


def test_a_job_submitted_now_is_enforced_by_default(client):
    """The exemption is for what was already in the system, not for new work."""
    _, token = _job_user(client, "newjob")

    res = _submit(client, token, gpu_memory_mb=2000)
    assert res.status_code == 201

    db = SessionLocal()
    try:
        job = db.query(models.Job).filter(models.Job.id == res.json()["id"]).first()
        assert job.gpu_memory_enforced is True
    finally:
        db.close()


def test_stopping_a_job_is_actually_written_down(client, monkeypatch):
    """Regression: the scheduler committed only when some job had *finished*.

    A job stopped for overrunning its VRAM was not on that list, so its
    container was really gone while the status change was rolled back, and
    the next pass reported it as having "stopped unexpectedly", losing the one
    message that said what to fix.
    """
    from services import container_manager, jobs as job_service

    user_id, _ = _job_user(client, "committed")
    monkeypatch.setattr(container_manager, "stop_job_container",
                        lambda job_id, timeout=10: True)
    monkeypatch.setattr(container_manager, "remove_job_container", lambda job_id: None)
    monkeypatch.setattr(container_manager, "job_container_state",
                        lambda job_id: {"running": True, "exit_code": None,
                                        "oom_killed": False, "status": "running"})
    monkeypatch.setattr(job_service, "job_gpu_memory_mb", lambda job: 30000)

    db = SessionLocal()
    try:
        job = models.Job(
            user_id=user_id, username="committed", script="job.sh", workdir="",
            gpu_count=1, gpu_memory_mb=4096, gpu_indices="0",
            gpu_memory_enforced=True,
            status=models.JobStatus.running, started_at=datetime.utcnow(),
        )
        db.add(job)
        db.commit()
        job_id = job.id
    finally:
        db.close()

    # A whole pass, through the same session lifecycle the scheduler uses.
    job_service.scheduler_pass()

    # Read it back on a fresh session: anything left uncommitted is gone now.
    db = SessionLocal()
    try:
        stored = db.query(models.Job).filter(models.Job.id == job_id).first()
        assert stored.status == models.JobStatus.failed
        assert "30000" in stored.message
    finally:
        db.close()


def test_a_note_on_a_running_job_is_written_down_too(client, monkeypatch):
    """The same rollback swallowed the warning on an exempt job."""
    from services import container_manager, jobs as job_service

    user_id, _ = _job_user(client, "notecommit")
    monkeypatch.setattr(container_manager, "job_container_state",
                        lambda job_id: {"running": True, "exit_code": None,
                                        "oom_killed": False, "status": "running"})
    monkeypatch.setattr(job_service, "job_gpu_memory_mb", lambda job: 39064)

    db = SessionLocal()
    try:
        job = models.Job(
            user_id=user_id, username="notecommit", script="job.sh", workdir="",
            gpu_count=1, gpu_memory_mb=20000, gpu_indices="0",
            gpu_memory_enforced=False,
            status=models.JobStatus.running, started_at=datetime.utcnow(),
        )
        db.add(job)
        db.commit()
        job_id = job.id
    finally:
        db.close()

    job_service.scheduler_pass()

    db = SessionLocal()
    try:
        stored = db.query(models.Job).filter(models.Job.id == job_id).first()
        assert stored.status == models.JobStatus.running, "it must not be stopped"
        assert stored.message and "39064" in stored.message
    finally:
        db.close()


def test_running_and_finished_jobs_are_asked_for_separately(client):
    """The dashboard shows them in two boxes: what is in flight is short and
    never paged, the history is long and paged."""
    user_id, token = _job_user(client, "twoboxes")

    db = SessionLocal()
    try:
        for i in range(3):
            db.add(models.Job(
                user_id=user_id, username="twoboxes", script=f"live{i}.sh",
                workdir="", gpu_count=0, status=models.JobStatus.running,
                started_at=datetime.utcnow(),
            ))
        for i in range(12):
            db.add(models.Job(
                user_id=user_id, username="twoboxes", script=f"done{i}.sh",
                workdir="", gpu_count=0, status=models.JobStatus.succeeded,
            ))
        db.commit()
    finally:
        db.close()

    live = client.get("/api/jobs", headers=_auth(token),
                      params={"active_only": True}).json()
    assert len(live["jobs"]) == 3
    assert all(j["status"] == "running" for j in live["jobs"])

    done = client.get("/api/jobs", headers=_auth(token),
                      params={"finished_only": True, "page": 1, "per_page": 10}).json()
    assert done["pagination"]["total"] == 12
    assert len(done["jobs"]) == 10
    assert not any(j["status"] == "running" for j in done["jobs"])


def test_the_vram_a_job_holds_is_recorded_before_it_is_a_problem(client, monkeypatch):
    """A job at 80% of its allowance is the one worth warning about, and that
    can only be shown if the figure is kept while everything is still fine."""
    from services import jobs as job_service

    user_id, token = _job_user(client, "measured")
    monkeypatch.setattr(job_service, "job_gpu_memory_mb", lambda job: 3000)

    db = SessionLocal()
    try:
        job = models.Job(
            user_id=user_id, username="measured", script="job.sh", workdir="",
            gpu_count=1, gpu_memory_mb=4096, gpu_indices="0",
            status=models.JobStatus.running, started_at=datetime.utcnow(),
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id

        # Well inside the allowance, so nothing is stopped …
        assert job_service._check_gpu_overrun(db, job) == "noted"
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        stored = db.query(models.Job).filter(models.Job.id == job_id).first()
        assert stored.status == models.JobStatus.running
        assert stored.gpu_memory_used_mb == 3000, "… but the figure is kept"
    finally:
        db.close()

    # And it reaches the dashboard next to the limit it is measured against.
    view = client.get(f"/api/jobs/{job_id}", headers=_auth(token)).json()
    assert view["gpu_memory_used_mb"] == 3000
    assert view["gpu_memory_allowance_mb"] == 5120


# ---------------------------------------------------------------------------
# The shared queue
# ---------------------------------------------------------------------------

def test_the_queue_shows_every_users_work(client):
    """Deciding whether to submit means knowing what is ahead of you, and your
    own jobs are the one part of that which does not matter."""
    alice_id, alice_token = _job_user(client, "queuealice")
    bob_id, bob_token = _job_user(client, "queuebob")

    db = SessionLocal()
    try:
        db.add(models.Job(user_id=alice_id, username="queuealice", script="a.sh",
                          workdir="", gpu_count=0, status=models.JobStatus.running,
                          started_at=datetime.utcnow()))
        db.add(models.Job(user_id=bob_id, username="queuebob", script="b.sh",
                          workdir="", gpu_count=0, status=models.JobStatus.queued))
        # Finished work is not in the queue; it is not ahead of anybody.
        db.add(models.Job(user_id=bob_id, username="queuebob", script="old.sh",
                          workdir="", gpu_count=0, status=models.JobStatus.succeeded))
        db.commit()
    finally:
        db.close()

    seen = client.get("/api/jobs/queue", headers=_auth(alice_token)).json()
    owners = {row["username"] for row in seen["queue"]}
    assert "queuealice" in owners and "queuebob" in owners
    assert seen["you"] == "queuealice"
    assert all(row["status"] in ("queued", "starting", "running")
               for row in seen["queue"])


def test_the_queue_carries_nothing_private(client):
    """It crosses user boundaries, so it may only carry what is public."""
    user_id, token = _job_user(client, "queueprivacy")

    db = SessionLocal()
    try:
        db.add(models.Job(user_id=user_id, username="queueprivacy",
                          script="secret-project/train.sh",
                          workdir="secret-project", gpu_count=0,
                          output_path="secret-project/output.1.out",
                          status=models.JobStatus.queued))
        db.commit()
    finally:
        db.close()

    row = next(r for r in client.get("/api/jobs/queue", headers=_auth(token)).json()["queue"]
               if r["username"] == "queueprivacy")
    for private in ("script", "workdir", "output", "output_tail", "message"):
        assert private not in row, f"{private} reached the shared queue"
    assert set(row) == {
        "id", "username", "name", "status", "gpu_count", "gpu_memory_mb",
        "gpus", "queue_position", "runtime_seconds", "waited_seconds",
    }


def test_another_users_job_cannot_be_opened(client):
    """The queue lists everyone's ids; looking inside one is another matter."""
    owner_id, _ = _job_user(client, "jobowner")
    _, other_token = _job_user(client, "jobsnooper")

    db = SessionLocal()
    try:
        job = models.Job(user_id=owner_id, username="jobowner", script="private.sh",
                         workdir="", gpu_count=0, status=models.JobStatus.running,
                         started_at=datetime.utcnow())
        db.add(job)
        db.commit()
        job_id = job.id
    finally:
        db.close()

    res = client.get(f"/api/jobs/{job_id}", headers=_auth(other_token))
    assert res.status_code == 403
    assert "jobowner" in res.json()["detail"]
    # And nothing of the job itself leaks in the refusal.
    assert "private.sh" not in res.text


def test_a_waiting_job_says_what_it_is_waiting_for(client, monkeypatch):
    """"Queued" on its own tells the user nothing they can act on."""
    from services import jobs as job_service

    user_id, token = _job_user(client, "whyqueued")

    # One GPU, nearly full: a job asking for more than is free cannot be placed.
    monkeypatch.setattr(job_service, "gpu_availability", lambda db: [
        {"index": 0, "uuid": None, "name": "test", "total_mb": 49140,
         "used_mb": 47000, "reserved_mb": 47000, "free_mb": 2000, "running_jobs": 1},
    ])

    db = SessionLocal()
    try:
        job = models.Job(user_id=user_id, username="whyqueued", script="job.sh",
                         workdir="", gpu_count=1, gpu_memory_mb=40000,
                         status=models.JobStatus.queued)
        db.add(job)
        db.commit()
        job_id = job.id
    finally:
        db.close()

    detail = client.get(f"/api/jobs/{job_id}", headers=_auth(token)).json()
    assert detail["status"] == "queued"
    reason = detail["blocked_reason"]
    assert reason and "40000" in reason and "2000" in reason, reason
    assert "GPU memory" in reason


def test_a_waiting_job_blocked_on_cpu_says_so(client, monkeypatch):
    from services import jobs as job_service

    user_id, token = _job_user(client, "whycpu")
    monkeypatch.setattr(job_service, "cpu_capacity",
                        lambda db: {"total_cores": 32, "free_cores": 0.5,
                                    "busy_cores": 31.5, "measured": True})

    db = SessionLocal()
    try:
        job = models.Job(user_id=user_id, username="whycpu", script="job.sh",
                         workdir="", gpu_count=0, gpu_memory_mb=0,
                         status=models.JobStatus.queued)
        db.add(job)
        db.commit()
        job_id = job.id
    finally:
        db.close()

    reason = client.get(f"/api/jobs/{job_id}", headers=_auth(token)).json()["blocked_reason"]
    assert reason and "CPU" in reason and "0.5" in reason, reason


def test_a_job_at_the_front_of_the_queue_is_not_blocked(client):
    """Nothing in the way must read as nothing in the way, not as silence."""
    user_id, token = _job_user(client, "nextinline")

    db = SessionLocal()
    try:
        job = models.Job(user_id=user_id, username="nextinline", script="job.sh",
                         workdir="", gpu_count=0, gpu_memory_mb=0,
                         status=models.JobStatus.queued)
        db.add(job)
        db.commit()
        job_id = job.id
    finally:
        db.close()

    detail = client.get(f"/api/jobs/{job_id}", headers=_auth(token)).json()
    assert detail["blocked_reason"] is None
    assert detail["waited_seconds"] is not None


def test_no_job_endpoint_answers_an_anonymous_caller(client):
    """The shared queue crosses user boundaries, which makes it exactly the
    endpoint somebody would be tempted to leave open.  None of them are."""
    user_id, _ = _job_user(client, "anonguard")

    db = SessionLocal()
    try:
        job = models.Job(user_id=user_id, username="anonguard", script="job.sh",
                         workdir="", gpu_count=0, status=models.JobStatus.running,
                         started_at=datetime.utcnow())
        db.add(job)
        db.commit()
        job_id = job.id
    finally:
        db.close()

    unauthenticated = [
        ("GET",    "/api/jobs", None),
        ("GET",    "/api/jobs/queue", None),
        ("GET",    f"/api/jobs/{job_id}", None),
        ("POST",   "/api/jobs", {"script": "job.sh"}),
        ("POST",   "/api/jobs/cancel", {"ids": [job_id]}),
        ("DELETE", f"/api/jobs/{job_id}", None),
    ]
    for method, path, body in unauthenticated:
        res = client.request(method, path, json=body)
        assert res.status_code == 401, f"{method} {path} answered {res.status_code}"
        assert "anonguard" not in res.text, f"{method} {path} leaked a username"

    # A made-up job token is refused the same way.
    bogus = {"X-Job-Token": "not-a-real-token"}
    assert client.get("/api/jobs/queue", headers=bogus).status_code == 401


def test_the_jobs_listing_is_scoped_to_the_caller(client):
    """`jobs` is the counterpart to `queue`: your own work, finished included,
    and nobody else's."""
    mine_id, my_token = _job_user(client, "scopedmine")
    theirs_id, _ = _job_user(client, "scopedtheirs")

    db = SessionLocal()
    try:
        db.add(models.Job(user_id=mine_id, username="scopedmine", script="mine.sh",
                          workdir="", gpu_count=0, status=models.JobStatus.succeeded))
        db.add(models.Job(user_id=theirs_id, username="scopedtheirs", script="theirs.sh",
                          workdir="", gpu_count=0, status=models.JobStatus.succeeded))
        db.commit()
    finally:
        db.close()

    listing = client.get("/api/jobs", headers=_auth(my_token),
                         params={"limit": 25}).json()
    owners = {j["user"] for j in listing["jobs"]}
    assert owners == {"scopedmine"}
    assert "theirs.sh" not in str(listing)


def test_a_warning_clears_when_the_job_comes_back_under(client, monkeypatch):
    """Regression: the check returned early once usage was fine again, so the
    note written while it was over stayed on the job for the rest of the run.

    A warning nobody can make go away is one people learn to ignore.
    """
    from services import jobs as job_service

    user_id, _ = _job_user(client, "backunder")
    held = {"mb": 43768}
    monkeypatch.setattr(job_service, "job_gpu_memory_mb", lambda job: held["mb"])

    db = SessionLocal()
    try:
        job = models.Job(
            user_id=user_id, username="backunder", script="job.sh", workdir="",
            gpu_count=1, gpu_memory_mb=20000, gpu_indices="0",
            gpu_memory_enforced=False,   # flagged, never stopped
            status=models.JobStatus.running, started_at=datetime.utcnow(),
        )
        db.add(job)
        db.commit()
        db.refresh(job)

        job_service._check_gpu_overrun(db, job)
        db.commit()
        db.refresh(job)
        assert "43768" in job.message, "it is over budget, so it is flagged"

        # The allocator releases its cache; the job is back inside its figure.
        held["mb"] = 16384
        job_service._check_gpu_overrun(db, job)
        db.commit()
        db.refresh(job)

        assert job.message is None, "the stale warning must go"
        assert job.gpu_memory_used_mb == 16384, "and the figure must follow"
        assert job.status == models.JobStatus.running
    finally:
        db.close()


def test_clearing_a_warning_leaves_other_notes_alone(client, monkeypatch):
    """`message` is shared, so only whoever wrote a note may clear it."""
    from services import jobs as job_service

    user_id, _ = _job_user(client, "othernote")
    monkeypatch.setattr(job_service, "job_gpu_memory_mb", lambda job: 1000)

    db = SessionLocal()
    try:
        job = models.Job(
            user_id=user_id, username="othernote", script="job.sh", workdir="",
            gpu_count=1, gpu_memory_mb=20000, gpu_indices="0",
            message="Its output file is 700 MB and still growing.",
            status=models.JobStatus.running, started_at=datetime.utcnow(),
        )
        db.add(job)
        db.commit()
        db.refresh(job)

        job_service._check_gpu_overrun(db, job)
        db.commit()
        db.refresh(job)

        assert job.message == "Its output file is 700 MB and still growing."
    finally:
        db.close()


def test_the_wording_an_earlier_version_wrote_is_cleared_too(client, monkeypatch):
    """Jobs running across the deployment carry the old phrasing."""
    from services import jobs as job_service

    user_id, _ = _job_user(client, "oldwording")
    monkeypatch.setattr(job_service, "job_gpu_memory_mb", lambda job: 16384)

    db = SessionLocal()
    try:
        job = models.Job(
            user_id=user_id, username="oldwording", script="job.sh", workdir="",
            gpu_count=1, gpu_memory_mb=20000, gpu_indices="0",
            message=("This job is using 39064 MB of GPU memory after reserving "
                     "20000 MB. The reservation is what the scheduler used to fit "
                     "other work beside it."),
            status=models.JobStatus.running, started_at=datetime.utcnow(),
        )
        db.add(job)
        db.commit()
        db.refresh(job)

        job_service._check_gpu_overrun(db, job)
        db.commit()
        db.refresh(job)

        assert job.message is None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Time budgets: GPU hours and CPU hours, per period
# ---------------------------------------------------------------------------

def test_a_budget_period_is_a_week_in_local_time(monkeypatch):
    """Weekly, and the week is the user's, not UTC's.

    A month is too long a period to be a lever: someone who burns their
    allowance on the 3rd is locked out for four weeks.  And at UTC+7 a reset
    computed in UTC would land at 07:00 on Monday morning, in the middle of a
    working day, which is the one time nobody wants the machine to change.
    """
    from datetime import datetime

    from config import settings
    from services import quota as quota_service

    monkeypatch.setattr(settings, "QUOTA_PERIOD", "week")
    monkeypatch.setattr(settings, "QUOTA_TZ_OFFSET_HOURS", 0)
    start, end = quota_service.period_bounds(datetime(2026, 9, 24, 3, 15))
    assert (start, end) == (datetime(2026, 9, 21), datetime(2026, 9, 28))
    assert quota_service.period_label(datetime(2026, 9, 24, 3, 15)) == "2026-W39"

    # Monday 00:00 in Hanoi is Sunday 17:00 UTC, so that is where the week
    # has to break.
    monkeypatch.setattr(settings, "QUOTA_TZ_OFFSET_HOURS", 7)
    assert quota_service.period_bounds(datetime(2026, 9, 27, 16, 30))[1] == \
        datetime(2026, 9, 27, 17, 0)
    assert quota_service.period_bounds(datetime(2026, 9, 27, 17, 30))[0] == \
        datetime(2026, 9, 27, 17, 0)

    monkeypatch.setattr(settings, "QUOTA_PERIOD", "month")
    monkeypatch.setattr(settings, "QUOTA_TZ_OFFSET_HOURS", 0)
    assert quota_service.period_bounds(datetime(2026, 9, 24))[0] == datetime(2026, 9, 1)


def test_hours_are_counted_for_the_part_inside_the_period(client, monkeypatch):
    """A run that straddles Monday belongs to both weeks, not to one of them.

    With a monthly period this hardly showed; with a weekly one, charging a
    whole run to the period it started in would hand out a free Sunday night
    every week, or bill for it twice.
    """
    from datetime import timedelta

    from config import settings
    from services import quota as quota_service

    monkeypatch.setattr(settings, "QUOTA_PERIOD", "week")
    monkeypatch.setattr(settings, "QUOTA_TZ_OFFSET_HOURS", 0)

    token = _login(client, "admin", ADMIN_PW)
    user_id = _make_user(client, token, "straddle")
    start, _ = quota_service.period_bounds()

    db = SessionLocal()
    try:
        user = db.get(models.User, user_id)
        # A four-hour job on two GPUs and four cores, half of it last week.
        db.add(models.UsageRecord(
            user_id=user.id, username=user.username, gpu_indices="0,1",
            gpu_count=2, cpu_cores=4.0, backend="job",
            started_at=start - timedelta(hours=2),
            ended_at=start + timedelta(hours=2),
            gpu_seconds=4 * 3600 * 2, cpu_seconds=4 * 3600 * 4,
            end_reason="succeeded",
        ))
        db.commit()

        used = quota_service.hours_used(db, user)
        assert used["gpu_hours"] == 4.0   # 2 hours x 2 GPUs
        assert used["cpu_hours"] == 8.0   # 2 hours x 4 cores

        # A job that has not finished writes no ledger row, so it is counted
        # from the Job table instead; without that a three-day run would show
        # as nothing used until the moment it ended.
        db.add(models.Job(
            user_id=user.id, username=user.username, script="job.sh", workdir="",
            gpu_count=1, gpu_memory_mb=1024, gpu_indices="0", cpu_cores=2.0,
            status=models.JobStatus.running,
            started_at=datetime.utcnow() - timedelta(hours=1),
        ))
        db.commit()
        used = quota_service.hours_used(db, user)
        assert used["gpu_hours"] == 5.0   # the four booked plus one live
        assert used["cpu_hours"] == 10.0
    finally:
        db.close()


def test_running_out_of_hours_refuses_a_job_and_nothing_else(client, monkeypatch):
    """The budget belongs to the queue: it refuses jobs, never the workspace.

    A workspace is the user's own seat at the machine and is already bounded by
    their assignment.  What a budget is for is the queue, where one person's
    backlog takes the machine from everybody else.  The 429 was also never
    covered by a test, which is how a refusal path stops working unnoticed.
    """
    from datetime import timedelta

    from config import settings
    from services import quota as quota_service

    monkeypatch.setattr(settings, "QUOTA_PERIOD", "week")
    token = _login(client, "admin", ADMIN_PW)
    user_id, user_token = _job_user(client, "spender")
    client.put(f"/api/admin/users/{user_id}", headers=_auth(token),
               json={"gpu_hours_quota": 1, "cpu_hours_quota": 4})

    start_of_period, _ = quota_service.period_bounds()
    db = SessionLocal()
    try:
        user = db.get(models.User, user_id)
        db.add(models.UsageRecord(
            user_id=user.id, username=user.username, gpu_indices="0",
            gpu_count=1, cpu_cores=0.0, backend="job",
            started_at=start_of_period + timedelta(minutes=1),
            ended_at=start_of_period + timedelta(minutes=121),
            gpu_seconds=7200, cpu_seconds=0.0, end_reason="succeeded",
        ))
        db.commit()
    finally:
        db.close()

    res = _submit(client, user_token)
    assert res.status_code == 429, res.text
    detail = res.json()["detail"]
    assert "GPU hours" in detail and "refills" in detail

    # The workspace is untouched by any of this.
    assert client.post("/api/user/me/jupyter/start",
                       headers=_auth(user_token), json={}).status_code == 200

    # The CPU budget refuses on its own, which is the whole point of replacing
    # the RLIMIT_CPU field that the container backend never read.
    client.put(f"/api/admin/users/{user_id}", headers=_auth(token),
               json={"gpu_hours_quota": 100})
    db = SessionLocal()
    try:
        user = db.get(models.User, user_id)
        db.add(models.UsageRecord(
            user_id=user.id, username=user.username, gpu_indices="",
            gpu_count=0, cpu_cores=4.0, backend="job",
            started_at=start_of_period + timedelta(minutes=1),
            ended_at=start_of_period + timedelta(minutes=121),
            gpu_seconds=0.0, cpu_seconds=2 * 3600 * 4, end_reason="succeeded",
        ))
        db.commit()
    finally:
        db.close()

    res = _submit(client, user_token)
    assert res.status_code == 429, res.text
    assert "CPU hours" in res.json()["detail"]


def test_an_interactive_session_costs_nothing_and_is_never_stopped(client, monkeypatch):
    """Time in a workspace is not charged, and the budget pass only reports.

    Charging it would make the budget a clock on somebody's own desk: the
    person who leaves a notebook open to read their results pays the same as
    the one running fifty jobs.  Idle sessions are the reaper's business.
    """
    from config import settings
    from services import quota as quota_service, reaper, session_backend

    monkeypatch.setattr(settings, "QUOTA_PERIOD", "week")
    token = _login(client, "admin", ADMIN_PW)
    user_id = _make_user(client, token, "sitting")
    client.put(f"/api/admin/users/{user_id}", headers=_auth(token),
               json={"gpu_hours_quota": 1, "cpu_hours_quota": 1})
    user_token = _login(client, "sitting", TRASH_PW)

    assert client.post("/api/user/me/jupyter/start",
                       headers=_auth(user_token), json={}).status_code == 200

    db = SessionLocal()
    try:
        user = db.get(models.User, user_id)
        # The session opened a ledger row holding GPUs and cores; none of it
        # counts, because it is not a job.
        assert quota_service.hours_used(db, user) == {"gpu_hours": 0.0, "cpu_hours": 0.0}
    finally:
        db.close()

    stopped = []
    monkeypatch.setattr(session_backend, "stop_session",
                        lambda *a, **kw: stopped.append(a) or True)
    # Even with the budget spent, the pass over live sessions only publishes.
    monkeypatch.setattr(quota_service, "hours_used",
                        lambda db, user, now=None: {"gpu_hours": 9.0, "cpu_hours": 9.0})
    spent = reaper.publish_time_budgets()

    assert any(row["user"] == "sitting" for row in spent)
    assert stopped == [], "a workspace is never stopped for a job budget"

    db = SessionLocal()
    try:
        session = (
            db.query(models.JupyterSession)
            .filter(models.JupyterSession.user_id == user_id)
            .first()
        )
        assert session.status == models.SessionStatus.running
    finally:
        db.close()


def test_a_running_job_goes_back_in_the_queue_when_the_budget_runs_out(client, monkeypatch):
    """Requeued rather than failed: the reason expires on its own.

    A job stopped for a reason that passes is a job somebody has to remember
    to submit again, and the platform already knows when it could run.
    """
    from config import settings
    from services import container_manager, jobs as job_service, quota as quota_service

    user_id, _ = _job_user(client, "outofhours")
    admin_token = _login(client, "admin", ADMIN_PW)
    client.put(f"/api/admin/users/{user_id}", headers=_auth(admin_token),
               json={"gpu_hours_quota": 1})

    monkeypatch.setattr(settings, "JOB_TIME_QUOTA_ACTION", "requeue")
    monkeypatch.setattr(quota_service, "hours_used",
                        lambda db, user, now=None: {"gpu_hours": 5.0, "cpu_hours": 0.0})
    monkeypatch.setattr(container_manager, "job_container_state",
                        lambda job_id: {"running": True, "exit_code": None,
                                        "oom_killed": False, "status": "running"})
    killed = []
    monkeypatch.setattr(container_manager, "stop_job_container",
                        lambda job_id, timeout=10: killed.append(job_id) or True)
    monkeypatch.setattr(container_manager, "remove_job_container",
                        lambda job_id: None)

    db = SessionLocal()
    try:
        job = models.Job(
            user_id=user_id, username="outofhours", script="job.sh", workdir="",
            gpu_count=1, gpu_memory_mb=1024, gpu_indices="0", cpu_cores=2.0,
            status=models.JobStatus.running,
            started_at=datetime.utcnow() - timedelta(hours=1),
        )
        db.add(job)
        db.commit()
        job_id = job.id
    finally:
        db.close()

    job_service._finalise_running(SessionLocal())

    db = SessionLocal()
    try:
        job = db.get(models.Job, job_id)
        assert job.status == models.JobStatus.queued
        assert job.container_id is None and job.started_at is None
        assert "back in the queue" in (job.message or "")
        assert killed == [job_id]

        # The hour it ran is still charged: a requeue that cost nothing would
        # be a way to use the machine for free.
        booked = (
            db.query(models.UsageRecord)
            .filter(models.UsageRecord.user_id == user_id,
                    models.UsageRecord.end_reason == "requeued")
            .first()
        )
        assert booked is not None
        assert round(booked.gpu_seconds / 3600.0) == 1

        # While the budget is still spent the scheduler holds it, and says so.
        user = db.get(models.User, user_id)
        assert quota_service.job_blocker(db, user)
        assert job_service.queued_blockers(db).get(job_id)
    finally:
        db.close()

    # Once the budget refills nothing else stands in its way.
    monkeypatch.setattr(quota_service, "hours_used",
                        lambda db, user, now=None: {"gpu_hours": 0.0, "cpu_hours": 0.0})
    db = SessionLocal()
    try:
        user = db.get(models.User, user_id)
        assert quota_service.job_blocker(db, user) is None
    finally:
        db.close()

    db = SessionLocal()
    try:
        db.query(models.Job).filter(models.Job.id == job_id).delete()
        db.commit()
    finally:
        db.close()


def test_a_cpu_job_is_paused_rather_than_restarted(client, monkeypatch):
    """Freezing a CPU job costs nothing; restarting it costs its progress.

    A paused container keeps its memory and its open files, so the work picks
    up where it stopped.  That is only safe when no GPU is involved: a frozen
    GPU job would hold VRAM nobody else could use until the budget refilled.
    """
    from config import settings
    from services import container_manager, jobs as job_service, quota as quota_service

    user_id, _ = _job_user(client, "frozen")
    admin_token = _login(client, "admin", ADMIN_PW)
    client.put(f"/api/admin/users/{user_id}", headers=_auth(admin_token),
               json={"cpu_hours_quota": 1})

    monkeypatch.setattr(settings, "JOB_TIME_QUOTA_CPU_ACTION", "pause")
    monkeypatch.setattr(quota_service, "hours_used",
                        lambda db, user, now=None: {"gpu_hours": 0.0, "cpu_hours": 9.0})
    monkeypatch.setattr(container_manager, "job_container_state",
                        lambda job_id: {"running": True, "paused": False,
                                        "exit_code": None, "oom_killed": False,
                                        "status": "running"})
    frozen, thawed, killed = [], [], []
    monkeypatch.setattr(container_manager, "pause_job_container",
                        lambda job_id: frozen.append(job_id) or True)
    monkeypatch.setattr(container_manager, "unpause_job_container",
                        lambda job_id: thawed.append(job_id) or True)
    monkeypatch.setattr(container_manager, "stop_job_container",
                        lambda job_id, timeout=10: killed.append(job_id) or True)
    monkeypatch.setattr(container_manager, "remove_job_container", lambda job_id: None)

    db = SessionLocal()
    try:
        job = models.Job(
            user_id=user_id, username="frozen", script="job.sh", workdir="",
            gpu_count=0, gpu_memory_mb=0, cpu_cores=4.0,
            status=models.JobStatus.running,
            started_at=datetime.utcnow() - timedelta(hours=2),
        )
        db.add(job)
        db.commit()
        job_id = job.id
    finally:
        db.close()

    job_service._finalise_running(SessionLocal())

    db = SessionLocal()
    try:
        job = db.get(models.Job, job_id)
        assert job.status == models.JobStatus.paused
        assert frozen == [job_id] and killed == []
        assert "paused" in (job.message or "").lower()
        # The clock stops: the two hours it ran are booked and its own runtime
        # keeps them, but `started_at` no longer runs away while it waits.
        assert job.started_at is None
        assert round(job.runtime_seconds / 3600.0) == 2
        booked = (
            db.query(models.UsageRecord)
            .filter(models.UsageRecord.user_id == user_id,
                    models.UsageRecord.end_reason == "paused")
            .first()
        )
        assert booked is not None and round(booked.cpu_seconds / 3600.0) == 8
    finally:
        db.close()

    # Once the budget refills the scheduler thaws it, and the new stretch
    # starts its clock again without losing the old one.
    monkeypatch.setattr(quota_service, "hours_used",
                        lambda db, user, now=None: {"gpu_hours": 0.0, "cpu_hours": 0.0})
    resumed = job_service._resume_paused(SessionLocal())
    assert [r["id"] for r in resumed] == [job_id]
    assert thawed == [job_id]

    db = SessionLocal()
    try:
        job = db.get(models.Job, job_id)
        assert job.status == models.JobStatus.running
        assert job.started_at is not None
        assert round(job.runtime_seconds / 3600.0) == 2   # kept across the pause
        db.query(models.Job).filter(models.Job.id == job_id).delete()
        db.commit()
    finally:
        db.close()


def test_paused_time_is_not_charged(client, monkeypatch):
    """The whole point of stopping the clock is that it stays stopped."""
    from config import settings
    from services import quota as quota_service

    monkeypatch.setattr(settings, "QUOTA_PERIOD", "week")
    token = _login(client, "admin", ADMIN_PW)
    user_id = _make_user(client, token, "stopclock")
    start, _ = quota_service.period_bounds()

    db = SessionLocal()
    try:
        user = db.get(models.User, user_id)
        # One hour of a 4-core job, booked when it was frozen an hour ago.
        db.add(models.UsageRecord(
            user_id=user.id, username=user.username, gpu_indices="",
            gpu_count=0, cpu_cores=4.0, backend="job",
            started_at=start + timedelta(hours=1),
            ended_at=start + timedelta(hours=2),
            gpu_seconds=0.0, cpu_seconds=4 * 3600.0, end_reason="paused",
        ))
        db.add(models.Job(
            user_id=user.id, username=user.username, script="job.sh", workdir="",
            gpu_count=0, gpu_memory_mb=0, cpu_cores=4.0,
            status=models.JobStatus.paused, started_at=None,
            runtime_seconds=3600.0,
        ))
        db.commit()

        used = quota_service.hours_used(db, user)
        assert used["cpu_hours"] == 4.0   # the hour it ran, and not a second more
    finally:
        db.close()


def test_the_history_can_be_filtered_and_sorted(client):
    """A year of jobs is only useful if you can find one in it."""
    _, token = _job_user(client, "historian")

    db = SessionLocal()
    try:
        user = db.query(models.User).filter(models.User.username == "historian").first()
        for n, (name, status) in enumerate([
            ("alpha-train", models.JobStatus.succeeded),
            ("beta-train", models.JobStatus.failed),
            ("gamma-prep", models.JobStatus.succeeded),
        ]):
            db.add(models.Job(
                user_id=user.id, username=user.username, name=name,
                script="job.sh", workdir="", gpu_count=0, gpu_memory_mb=0,
                status=status,
                created_at=datetime.utcnow() - timedelta(hours=3 - n),
                finished_at=datetime.utcnow() - timedelta(hours=2 - n),
                runtime_seconds=(n + 1) * 600,
            ))
        db.commit()
    finally:
        db.close()

    def names(**params):
        res = client.get("/api/jobs", headers=_auth(token),
                         params={"finished_only": "true", **params})
        assert res.status_code == 200, res.text
        return [j["name"] for j in res.json()["jobs"]]

    assert names(status="failed") == ["beta-train"]
    assert names(q="train") == ["beta-train", "alpha-train"]      # newest first
    assert names(q="TRAIN") == ["beta-train", "alpha-train"]      # case does not matter
    assert names(sort="name", order="asc")[:3] == \
        ["alpha-train", "beta-train", "gamma-prep"]
    assert names(sort="runtime", order="desc")[0] == "gamma-prep"

    # Filtering narrows the count the pager is built from, not just the page.
    res = client.get("/api/jobs", headers=_auth(token),
                     params={"finished_only": "true", "status": "succeeded"})
    assert res.json()["pagination"]["total"] == 2

    # Both times the table shows are in the payload.
    row = res.json()["jobs"][0]
    assert row["created_at"] and row["finished_at"]


def test_a_failed_gpu_read_is_not_the_same_as_no_gpus(monkeypatch):
    """The cards do not vanish because one nvidia-smi call did.

    This is what a container looks like after systemd reapplies its device
    policy and revokes /dev/nvidia*: the host is fine, the nodes are still
    there, and every call from inside fails. Reporting that as "no GPUs" sent
    administrators looking for a dead card, and quietly told the scheduler the
    machine had no capacity.
    """
    from services import gpu_monitor

    good = ("0, GPU-abc, NVIDIA GeForce RTX 4090, 49140, 8000, 41140, 10, 5, 45, 120.5\n")
    monkeypatch.setattr(gpu_monitor, "_run", lambda cmd, timeout=10: (
        good if "--query-gpu=index,uuid,name" in " ".join(cmd) else ""
    ))
    monkeypatch.setattr(gpu_monitor, "platform_gpu_usage", lambda: {})
    monkeypatch.setattr(gpu_monitor, "_container_processes", lambda: {}, raising=False)

    live = gpu_monitor.get_gpu_status()
    assert [g["index"] for g in live] == [0]
    assert live[0]["stale"] is False
    assert gpu_monitor.telemetry()["ok"] is True

    # Now nvidia-smi starts failing the way it does when access is revoked.
    def broken(cmd, timeout=10):
        gpu_monitor._LAST_ERROR["message"] = "Failed to initialize NVML: Unknown Error"
        return None

    monkeypatch.setattr(gpu_monitor, "_run", broken)

    after = gpu_monitor.get_gpu_status()
    assert [g["index"] for g in after] == [0], "the last good reading is kept"
    assert after[0]["stale"] is True

    state = gpu_monitor.telemetry()
    assert state["ok"] is False
    assert state["ever_seen"] is True
    assert "NVML" in (state["error"] or "")

    # And the scheduler is told not to place work against it.
    from services import jobs as job_service
    assert job_service._pool_gpus() == []


def test_the_device_nodes_of_an_assignment_are_named_exactly(monkeypatch):
    """Only the assigned card's node, so the isolation is unchanged.

    Naming the nodes is what makes the daemon record them, and the daemon
    recording them is what makes systemd reapply rather than revoke them. A
    wrong minor here would hand somebody another user's card, so the mapping
    comes from the driver rather than from the assumption that index == minor.
    """
    from services import container_manager, gpu_monitor

    monkeypatch.setattr(gpu_monitor, "device_minor_by_index", lambda: {0: 3, 1: 2})
    monkeypatch.setattr(container_manager.glob, "glob",
                        lambda pattern: ["/dev/nvidiactl", "/dev/nvidia-uvm",
                                         "/dev/nvidia-uvm-tools", "/dev/nvidia3"])

    nodes = container_manager.nvidia_device_nodes(["1"])
    assert "/dev/nvidia2" in nodes, "GPU 1 is minor 2 on this host"
    assert "/dev/nvidia3" not in nodes, "the other card must not be listed"
    assert "/dev/nvidiactl" in nodes and "/dev/nvidia-uvm" in nodes


def test_the_admin_queue_is_paged_filtered_and_counted_whole(client):
    """An administrator sees every user's jobs, so the list needs handles.

    The counts are the part worth testing: they are taken over the whole
    system, not over the page being looked at, or "3 running" turns into
    "0 running" the moment somebody pages through the history.
    """
    token = _login(client, "admin", ADMIN_PW)
    owner, _ = _job_user(client, "queuewatch")

    db = SessionLocal()
    try:
        user = db.get(models.User, owner)
        for n in range(6):
            db.add(models.Job(
                user_id=user.id, username=user.username, name=f"sweep-{n}",
                script="job.sh", workdir="", gpu_count=0, gpu_memory_mb=0,
                status=models.JobStatus.succeeded if n % 2 else models.JobStatus.failed,
                created_at=datetime.utcnow() - timedelta(hours=6 - n),
                finished_at=datetime.utcnow() - timedelta(hours=5 - n),
                runtime_seconds=(n + 1) * 60,
            ))
        db.add(models.Job(
            user_id=user.id, username=user.username, name="the-live-one",
            script="job.sh", workdir="", gpu_count=0, gpu_memory_mb=0,
            status=models.JobStatus.running, started_at=datetime.utcnow(),
        ))
        db.commit()
    finally:
        db.close()

    def get(**params):
        res = client.get("/api/admin/jobs", headers=_auth(token), params=params)
        assert res.status_code == 200, res.text
        return res.json()

    # Other tests leave work of their own behind, so this asks whether the
    # live job is in the active list rather than whether it is alone in it.
    active = get(active_only="true", per_page=50)
    assert "the-live-one" in [j["name"] for j in active["jobs"]]
    assert all(j["status"] in ("queued", "starting", "running", "paused")
               for j in active["jobs"])
    assert active["counts"]["running"] >= 1
    # The first table carries the capacity figures; the second asks without.
    assert "cpu" in active and "fair_share" in active

    page1 = get(finished_only="true", per_page=2, capacity="false")
    assert len(page1["jobs"]) == 2
    assert page1["pagination"]["total"] >= 6
    assert "cpu" not in page1, "the second table should not recompute capacity"
    # Counts still describe the machine, not the two rows on this page.
    assert page1["counts"]["running"] >= 1

    assert {j["status"] for j in get(finished_only="true", status="failed",
                                    capacity="false")["jobs"]} == {"failed"}
    assert {j["user"] for j in get(q="queuewatch", capacity="false")["jobs"]} == {"queuewatch"}

    longest = get(finished_only="true", q="sweep", sort="runtime", order="desc",
                  per_page=1, capacity="false")["jobs"]
    assert longest[0]["name"] == "sweep-5"

    # A page past the end lands on the last real one rather than going blank.
    far = get(finished_only="true", page=999, per_page=2, capacity="false")
    assert far["pagination"]["page"] == far["pagination"]["pages"]
    assert far["jobs"]

    db = SessionLocal()
    try:
        db.query(models.Job).filter(models.Job.user_id == owner).delete()
        db.commit()
    finally:
        db.close()


def test_the_peak_survives_a_job_that_released_its_memory(client, monkeypatch):
    """The live figure is whatever the last reading caught; the peak is what
    still answers "what did this run need?" after the job has ended.

    A job that frees its allocator before exiting reads zero on the way out,
    which is true at that instant and useless to anyone asking afterwards.
    """
    from services import jobs as job_service

    user_id, _ = _job_user(client, "peaky")
    readings = iter([2048, 9000, 0])
    monkeypatch.setattr(job_service, "job_gpu_memory_mb",
                        lambda job: next(readings))

    db = SessionLocal()
    try:
        job = models.Job(
            user_id=user_id, username="peaky", script="job.sh", workdir="",
            gpu_count=1, gpu_memory_mb=20000, gpu_indices="0",
            status=models.JobStatus.running, started_at=datetime.utcnow(),
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id

        for _ in range(3):
            job_service._check_gpu_overrun(db, job)
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        stored = db.query(models.Job).filter(models.Job.id == job_id).first()
        assert stored.gpu_memory_used_mb == 0, "the live figure follows the job down"
        assert stored.gpu_memory_peak_mb == 9000, "the peak does not"
        db.query(models.Job).filter(models.Job.id == job_id).delete()
        db.commit()
    finally:
        db.close()


def test_the_host_scan_attributes_vram_to_containers(monkeypatch, tmp_path):
    """One nvidia-smi for the whole host, mapped back through /proc.

    Two processes in one container are summed; a process the backend cannot
    trace is skipped rather than charged to somebody.
    """
    from services import gpu_scan

    cid = "a" * 64
    proc_dir = tmp_path
    for pid, text in (
        ("100", f"0::/system.slice/docker-{cid}.scope"),
        ("101", f"0::/docker/{cid}"),
        ("102", "0::/user.slice/session-3.scope"),      # not in a container
    ):
        (proc_dir / pid).mkdir()
        (proc_dir / pid / "cgroup").write_text(text)

    monkeypatch.setattr(gpu_scan, "_container_of", lambda pid: (
        None if pid == "102" else cid))

    class _Done:
        returncode = 0
        stdout = "100, 3000\n101, 1500\n102, 8000\n"
        stderr = ""

    monkeypatch.setattr(gpu_scan.subprocess, "run", lambda *a, **k: _Done())

    assert gpu_scan.refresh() == {cid: 4500}, "the two in one container are summed"
    assert gpu_scan.usable()
    assert gpu_scan.container_mb(cid) == 4500
    # A container with no compute process holds nothing; that is an answer, and
    # the enforcement code needs it to be one.
    assert gpu_scan.container_mb("b" * 64) == 0


def test_a_scan_that_can_see_nothing_says_so_rather_than_reporting_zero(monkeypatch):
    """Without the host PID namespace the driver's PIDs resolve to no
    container.  Reporting zero for every job would be worse than reporting
    nothing, because zero is a number the enforcement code believes."""
    from services import gpu_scan

    class _Done:
        returncode = 0
        stdout = "100, 3000\n"
        stderr = ""

    monkeypatch.setattr(gpu_scan.subprocess, "run", lambda *a, **k: _Done())
    monkeypatch.setattr(gpu_scan, "_container_of", lambda pid: None)

    assert gpu_scan.refresh() == {}
    assert not gpu_scan.usable()
    assert gpu_scan.container_mb("c" * 64) is None, "None, never 0"


def test_a_broken_scan_is_not_asked_once_a_second(monkeypatch):
    """A wedged nvidia-smi answers slowly; asking it every pass only waits.

    After a failure the next attempt is held back to the interval the caller
    names, so a host with no working driver costs one call per interval and
    not one per pass.
    """
    from services import gpu_scan

    calls = []

    class _Broken:
        returncode = 9
        stdout = ""
        stderr = "driver not loaded"

    def _run(*a, **k):
        calls.append(1)
        return _Broken()

    monkeypatch.setattr(gpu_scan.subprocess, "run", _run)
    monkeypatch.setattr(gpu_scan, "_usable", False)
    monkeypatch.setattr(gpu_scan, "_last_attempt", 0.0)

    for _ in range(10):
        gpu_scan.refresh(retry_after_failure=10)

    assert len(calls) == 1, "ten passes, one attempt"
    assert not gpu_scan.usable()


def test_the_guard_stands_down_rather_than_exec_into_every_job(client, monkeypatch):
    """With no scan, reading a job costs a docker exec; doing that for every
    job once a second is the expense the guard exists to avoid.  The scheduler
    pass checks the same jobs on its own cycle, so the guard does nothing."""
    from services import gpu_scan, jobs as job_service

    monkeypatch.setattr(gpu_scan, "refresh", lambda **kw: {})
    monkeypatch.setattr(gpu_scan, "usable", lambda *a, **k: False)

    def _must_not_run(job):
        raise AssertionError("the guard read a job with no scan available")

    monkeypatch.setattr(job_service, "job_gpu_memory_mb", _must_not_run)

    assert job_service.gpu_guard_pass() == {
        "checked": 0, "stopped": [], "scan": "unusable"}


def test_placement_holds_back_the_grace_a_job_may_still_grow_into(client):
    """A job inside its allowance is deliberately not stopped, so that margin
    is not free space.  Promising it to a second job puts the second one in
    the space the first may legally take, and the second is the one that dies.

    Measured as a delta, because the card may already carry other jobs.
    """
    from config import settings
    from services import jobs as job_service

    user_id, _ = _job_user(client, "entitled")

    db = SessionLocal()
    job_id = None
    try:
        before = {g["index"]: g for g in job_service.gpu_availability(db)}[0]

        job = models.Job(
            user_id=user_id, username="entitled", script="job.sh", workdir="",
            gpu_count=1, gpu_memory_mb=20000, gpu_indices="0",
            status=models.JobStatus.running, started_at=datetime.utcnow(),
        )
        db.add(job)
        db.commit()
        job_id = job.id

        after = {g["index"]: g for g in job_service.gpu_availability(db)}[0]

        assert after["reserved_mb"] - before["reserved_mb"] == 20000, \
            "what the user asked for, unchanged"
        # max(1.1 * 20000, 20000 + 1024) = 22000
        assert after["entitled_mb"] - before["entitled_mb"] == 22000, \
            "what it may hold before being stopped"
        # Checked against the formula rather than as a delta: the card in the
        # test environment may already be full, and free_mb is clamped at zero.
        assert after["free_mb"] == max(0, after["total_mb"]
                                       - max(after["used_mb"], after["entitled_mb"])
                                       - settings.JOB_GPU_HEADROOM_MB), \
            "free space is measured against the allowance, not the request"
    finally:
        if job_id is not None:
            db.query(models.Job).filter(models.Job.id == job_id).delete()
            db.commit()
        db.close()


def test_a_job_that_never_touched_the_card_has_no_peak(client, monkeypatch):
    """A zero is not a high-water mark.  Recorded as one, a job that asked for a
    card and never used it would read "0.0 GB peak" on screen, which claims a
    measurement where there was only an absence."""
    from services import jobs as job_service

    user_id, _ = _job_user(client, "untouched")
    monkeypatch.setattr(job_service, "job_gpu_memory_mb", lambda job: 0)

    db = SessionLocal()
    try:
        job = models.Job(
            user_id=user_id, username="untouched", script="job.sh", workdir="",
            gpu_count=1, gpu_memory_mb=4000, gpu_indices="0",
            status=models.JobStatus.running, started_at=datetime.utcnow(),
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.id

        for _ in range(3):
            job_service._check_gpu_overrun(db, job)
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        stored = db.query(models.Job).filter(models.Job.id == job_id).first()
        assert stored.gpu_memory_used_mb == 0, "it was measured, and held nothing"
        assert stored.gpu_memory_peak_mb is None, "but nothing is not a peak"
        db.query(models.Job).filter(models.Job.id == job_id).delete()
        db.commit()
    finally:
        db.close()


def _fake_container(host_config, cgroup_text, cid="c" * 64):
    """A container that answers exec_run with canned cgroup files."""
    class _C:
        id = cid
        attrs = {"HostConfig": host_config}

        def exec_run(self, cmd):
            return 0, cgroup_text.encode()
    return _C()


def test_the_audit_catches_swap_the_kernel_never_disabled():
    """Docker records MemorySwap == Memory and believes swap is off; the cgroup
    says otherwise.  `docker inspect` cannot show this, which is the whole
    reason the check reads the kernel instead."""
    from services import limit_audit

    limit_audit._cache.clear()
    c = _fake_container(
        {"Memory": 20971520000, "MemorySwap": 20971520000,
         "NanoCpus": 0, "PidsLimit": 0},
        "@@memory.max\n20971520000\n@@memory.swap.max\nmax\n"
        "@@cpu.max\nmax 100000\n@@pids.max\nmax\n@@io.max\n\n@@gpu.nodes\n",
    )
    found = {f["setting"]: f for f in limit_audit.audit(c)}
    assert "swap" in found, "a soft memory cap has to be reported"
    assert found["swap"]["configured"] == "disabled"
    assert found["swap"]["kernel"] == "unlimited"


def test_the_audit_says_nothing_when_the_kernel_agrees():
    """The normal case renders no banner, so it has to produce no findings."""
    from services import limit_audit

    limit_audit._cache.clear()
    c = _fake_container(
        {"Memory": 10536091648, "MemorySwap": 10536091648,
         "NanoCpus": 10 ** 10, "PidsLimit": 512,
         "BlkioDeviceReadBps": [{"Rate": 157286400}]},
        "@@memory.max\n10536091648\n@@memory.swap.max\n0\n"
        "@@cpu.max\n1000000 100000\n@@pids.max\n512\n"
        "@@io.max\n259:0 rbps=157286400 wbps=83886080\n@@gpu.nodes\n",
        cid="d" * 64,
    )
    assert limit_audit.audit(c) == []


def test_the_audit_catches_a_gpu_revoked_from_a_running_container():
    """The NVIDIA hook adds the device nodes behind the daemon's back, so
    systemd can take them away from a container that is still running and
    Docker will go on reporting the device request it was given."""
    from services import limit_audit

    limit_audit._cache.clear()
    c = _fake_container(
        {"Memory": 0, "MemorySwap": 0, "NanoCpus": 0, "PidsLimit": 0,
         "DeviceRequests": [{"DeviceIDs": ["GPU-abc"]}]},
        "@@memory.max\nmax\n@@memory.swap.max\nmax\n@@cpu.max\nmax 100000\n"
        "@@pids.max\nmax\n@@io.max\n\n@@gpu.nodes\n",
        cid="e" * 64,
    )
    found = {f["setting"]: f for f in limit_audit.audit(c)}
    assert "GPU devices" in found
    assert "revoked" in found["GPU devices"]["detail"]


def test_a_container_that_cannot_be_read_is_not_accused():
    """Claiming a divergence because the exec failed would put a red banner in
    front of an administrator over nothing at all."""
    from services import limit_audit

    limit_audit._cache.clear()

    class _Dead:
        id = "f" * 64
        attrs = {"HostConfig": {"Memory": 1, "MemorySwap": 1}}

        def exec_run(self, cmd):
            raise RuntimeError("container is gone")

    assert limit_audit.audit(_Dead()) == []


def test_a_workspace_with_no_gpu_is_still_audited():
    """`ls /dev/nvidia*` exits non-zero when there is no GPU, and the exit code
    of the last command is the exit code of the script.  Read as failure, that
    silently excused every GPU-less workspace from the audit."""
    from services import limit_audit

    limit_audit._cache.clear()
    c = _fake_container(
        {"Memory": 20971520000, "MemorySwap": 20971520000,
         "NanoCpus": 0, "PidsLimit": 0},
        # No GPU nodes, and a swap divergence that must still be found.
        "@@memory.max\n20971520000\n@@memory.swap.max\nmax\n"
        "@@cpu.max\nmax 100000\n@@pids.max\nmax\n@@io.max\n\n@@gpu.nodes\n",
        cid="1" * 64,
    )
    found = {f["setting"] for f in limit_audit.audit(c)}
    assert "swap" in found, "a GPU-less workspace is audited like any other"
    assert "GPU devices" not in found, "and is not accused of losing a GPU it never had"
