#!/usr/bin/env python
"""Database initialisation script.

Creates all tables, applies lightweight column migrations, and seeds a default
admin account if one does not already exist.  Idempotent, safe to run on every
container start.

Usage::

    python init_db.py
"""

import os
import sys

from sqlalchemy.exc import OperationalError

# Ensure the data directory exists before SQLAlchemy opens the file.
# 0700, it stores password hashes and encrypted session secrets.
os.makedirs("data", mode=0o700, exist_ok=True)
try:
    os.chmod("data", 0o700)
except OSError:
    pass

# All imports that trigger ORM / DB wiring happen after the directory exists
from database import SessionLocal, engine  # noqa: E402
import models  # noqa: E402
from auth import get_password_hash  # noqa: E402


# Columns added after the first release: (table, column, DDL type)
_COLUMN_MIGRATIONS = [
    ("jupyter_sessions", "container_id",  "VARCHAR(128)"),
    ("jupyter_sessions", "ssh_port",      "INTEGER"),
    ("jupyter_sessions", "ssh_password",  "VARCHAR(512)"),
    ("jupyter_sessions", "image",         "VARCHAR(255)"),
    ("users",            "ssh_public_key", "VARCHAR(1024)"),
    ("users",            "hashed_jupyter_password", "VARCHAR(255)"),
    ("users",            "token_version",  "INTEGER NOT NULL DEFAULT 0"),
    ("users",            "disk_quota_mb",  "INTEGER"),
    ("users",            "gpu_hours_quota", "FLOAT"),
    ("users",            "preferred_image", "VARCHAR(255)"),
    ("gpu_assignments",  "cpu_cores",      "FLOAT"),
    ("users",            "account_jupyter_hash", "VARCHAR(255)"),
    ("users",            "unix_password_hash",   "VARCHAR(255)"),
    ("users",            "job_token_hash",       "VARCHAR(64)"),
    ("users",            "home_path",            "VARCHAR(512)"),
    ("usage_records",    "cpu_cores",   "FLOAT NOT NULL DEFAULT 0"),
    ("usage_records",    "cpu_seconds", "FLOAT NOT NULL DEFAULT 0"),
    ("jobs",             "cpu_cores",   "FLOAT NOT NULL DEFAULT 0"),
    ("users",            "deleted_at",         "DATETIME"),
    ("users",            "archived_workspace", "VARCHAR(255)"),
    ("users",            "restore_state",      "TEXT"),
    ("gpu_assignments",  "max_processes",      "INTEGER"),
    # DEFAULT 0 on purpose: the backfill is what exempts jobs that were already
    # submitted when the GPU reservation became binding.  New rows get their
    # value from the ORM (True), so the column default only ever applies here.
    ("jobs",             "gpu_memory_enforced", "BOOLEAN NOT NULL DEFAULT 0"),
    ("jobs",             "gpu_memory_used_mb",  "INTEGER"),
    ("jobs",             "gpu_memory_peak_mb",  "INTEGER"),
    ("users",            "cpu_hours_quota",     "FLOAT"),
    ("jobs",             "runtime_seconds",     "FLOAT NOT NULL DEFAULT 0"),
]


def create_tables() -> None:
    """Create all tables declared in models.py (no-op if they already exist)."""
    try:
        models.Base.metadata.create_all(bind=engine)
        _migrate_schema()
        print("[init_db] Database tables verified / created.")
    except OperationalError as exc:
        print(f"[init_db] ERROR: Could not create tables – {exc}", file=sys.stderr)
        sys.exit(1)


def _migrate_schema() -> None:
    """Add columns introduced by later versions to an existing database."""
    from sqlalchemy import text

    with engine.connect() as conn:
        for table, column, ddl in _COLUMN_MIGRATIONS:
            existing = {
                row[1]
                for row in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
            }
            if not existing:
                continue  # table not created yet (fresh DB handled by create_all)
            if column in existing:
                continue
            print(f"[init_db] Migrating: adding {table}.{column}")
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
        conn.commit()

    _encrypt_legacy_secrets()
    _fix_jupyter_password_hashes()
    _rescue_cpu_core_values()
    _backfill_job_runtime()
    _clear_zero_gpu_peaks()


def _backfill_job_runtime() -> None:
    """Fill in ``jobs.runtime_seconds`` for jobs that ended before it existed.

    The column holds how long a job actually ran, summed over its stretches,
    which is what the history table sorts by.  A job that finished before the
    column was added has a zero in it and would sort as if it had run for no
    time at all, so its one stretch is reconstructed from the timestamps it
    does have.  Only rows that were never written by the running platform are
    touched, and only once.
    """
    from sqlalchemy import text

    if engine.dialect.name != "sqlite":
        return  # julianday() is SQLite's; elsewhere the column just fills up
    with engine.connect() as conn:
        try:
            filled = conn.execute(text(
                "UPDATE jobs SET runtime_seconds = "
                "  (julianday(finished_at) - julianday(started_at)) * 86400.0 "
                "WHERE runtime_seconds = 0 AND started_at IS NOT NULL "
                "  AND finished_at IS NOT NULL AND finished_at > started_at"
            )).rowcount
            conn.commit()
        except OperationalError:
            return  # table not there yet on a first run
    if filled:
        print(f"[init_db] Migrating: runtime filled in for {filled} finished job(s)")


def _clear_zero_gpu_peaks() -> None:
    """Drop the zeros an earlier build wrote into jobs.gpu_memory_peak_mb.

    A zero is not a high-water mark.  The first version of the peak recorded
    whatever the first scan returned, including nothing at all, so a job that
    asked for a card and never touched it ended up claiming a measured peak of
    0.0 GB instead of admitting there was no measurement to report.  Null is
    what that should have been, and is what the tables render as a dash.
    """
    from sqlalchemy import text

    with engine.connect() as conn:
        try:
            cleared = conn.execute(text(
                "UPDATE jobs SET gpu_memory_peak_mb = NULL "
                "WHERE gpu_memory_peak_mb = 0"
            )).rowcount
            conn.commit()
        except OperationalError:
            return  # column or table not there yet on a first run
    if cleared:
        print(f"[init_db] Migrating: cleared a zero GPU peak on {cleared} job(s)")


def _encrypt_legacy_secrets() -> None:
    """Encrypt session tokens / SSH passwords written before v2.

    Rows created by the previous version stored both in plaintext; leaving them
    that way would defeat the point of encrypting new ones.
    """
    from services import crypto

    db = SessionLocal()
    try:
        touched = 0
        for session in db.query(models.JupyterSession).all():
            if session.token and not session.token.startswith("enc:v1:"):
                session.token = crypto.encrypt(session.token)
                touched += 1
            if session.ssh_password and not session.ssh_password.startswith("enc:v1:"):
                session.ssh_password = crypto.encrypt(session.ssh_password)
                touched += 1
        if touched:
            db.commit()
            print(f"[init_db] Encrypted {touched} legacy session secret(s).")
    except Exception as exc:  # noqa: BLE001 (never block startup on this)
        print(f"[init_db] WARNING: could not encrypt legacy secrets – {exc}", file=sys.stderr)
        db.rollback()
    finally:
        db.close()


def _fix_jupyter_password_hashes() -> None:
    """Add the ``argon2:`` prefix to Jupyter password hashes stored without it.

    Hashes written before this fix made Jupyter reject the correct password:
    without the prefix its passwd_check() falls through to the legacy
    "algorithm:salt:digest" branch and always returns False.
    """
    from auth import normalize_jupyter_hash

    db = SessionLocal()
    try:
        fixed = 0
        for user in db.query(models.User).filter(
            models.User.hashed_jupyter_password.isnot(None)
        ):
            corrected = normalize_jupyter_hash(user.hashed_jupyter_password)
            if corrected != user.hashed_jupyter_password:
                user.hashed_jupyter_password = corrected
                fixed += 1
        if fixed:
            db.commit()
            print(f"[init_db] Repaired {fixed} Jupyter password hash(es) "
                  "(missing argon2: prefix).")
    except Exception as exc:  # noqa: BLE001 (never block startup on this)
        print(f"[init_db] WARNING: could not repair Jupyter hashes – {exc}", file=sys.stderr)
        db.rollback()
    finally:
        db.close()


def _rescue_cpu_core_values() -> None:
    """Re-read old ``cpu_limit_seconds`` values as the core counts they meant.

    Until now the assignment form's only CPU field was "CPU limit (seconds)",
    and the container backend turned it into cores with ``seconds / 3600``
    clamped to a 0.25 floor.  An admin typing 4 for "four cores" got a
    container throttled to a quarter of a core.

    A value no larger than the host's core count cannot plausibly be a
    CPU-seconds budget (four seconds of CPU would end a session instantly), so
    it is moved to ``cpu_cores``.  Larger values are left alone: those are
    genuine RLIMIT_CPU budgets for the process backend.
    """
    import os as _os

    host_cores = _os.cpu_count() or 8
    db = SessionLocal()
    try:
        rescued = []
        for assignment in db.query(models.GpuAssignment).all():
            seconds = assignment.cpu_limit_seconds
            if assignment.cpu_cores or not seconds or seconds > host_cores:
                continue
            assignment.cpu_cores = float(seconds)
            assignment.cpu_limit_seconds = None
            db.add(assignment)
            rescued.append(f"user_id={assignment.user_id}: {seconds}s -> {seconds} cores")
        if rescued:
            db.commit()
            print("[init_db] Re-read mis-entered CPU limits as core counts:")
            for line in rescued:
                print(f"           {line}")
    except Exception as exc:  # noqa: BLE001 (never block startup on this)
        print(f"[init_db] WARNING: could not rescue CPU values – {exc}", file=sys.stderr)
        db.rollback()
    finally:
        db.close()


def create_admin_user() -> None:
    """Create the default admin user if it does not already exist."""
    db = SessionLocal()
    try:
        existing = db.query(models.User).filter(models.User.username == "admin").first()
        if existing is not None:
            print("[init_db] Admin user already exists – skipping creation.")
            return

        admin_password = os.environ.get("ADMIN_PASSWORD", "admin123")

        from auth import derive_credentials

        admin = models.User(
            username="admin",
            email="admin@localhost",
            hashed_password=get_password_hash(admin_password),
            **derive_credentials(admin_password),
            full_name="System Administrator",
            is_admin=True,
            is_active=True,
        )
        db.add(admin)
        db.commit()

        separator = "=" * 54
        print(separator)
        print("  Default admin account created successfully!")
        print("  Username : admin")
        print(f"  Password : {admin_password}")
        print(separator)
        print("  IMPORTANT: Change this password immediately after")
        print("  first login, especially in production environments.")
        print(separator)

    except Exception as exc:  # noqa: BLE001
        db.rollback()
        print(f"[init_db] ERROR: Could not create admin user – {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        db.close()


def main() -> None:
    print("[init_db] Initialising GPU Management Platform database…")
    create_tables()
    create_admin_user()
    print("[init_db] Initialisation complete.")


if __name__ == "__main__":
    main()
