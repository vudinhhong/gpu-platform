import enum
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, Enum, Float, ForeignKey, Index,
    Integer, String, Text,
)
from sqlalchemy.orm import relationship

from database import Base


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class JobStatus(str, enum.Enum):
    """Lifecycle of a submitted batch job."""
    queued    = "queued"      # waiting for a GPU with room
    starting  = "starting"    # container being created
    running   = "running"
    # Frozen mid-run because its owner ran out of CPU hours, and resumed by
    # the scheduler when the budget refills.  Only CPU-only jobs are ever
    # paused: a frozen container keeps its memory, which is somebody's RAM on
    # a CPU job and somebody's whole card on a GPU one.
    paused    = "paused"
    succeeded = "succeeded"   # exit code 0
    failed    = "failed"      # non-zero exit, or could not start
    cancelled = "cancelled"   # cancelled by its owner or an admin
    timeout   = "timeout"     # exceeded its runtime limit


class SessionStatus(str, enum.Enum):
    """Lifecycle states for a JupyterLab process."""
    stopped  = "stopped"
    starting = "starting"
    running  = "running"
    error    = "error"


# ---------------------------------------------------------------------------
# ORM Models
# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id              = Column(Integer, primary_key=True, index=True)
    username        = Column(String(64),  unique=True, index=True, nullable=False)
    email           = Column(String(255), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    full_name       = Column(String(255), nullable=True)
    is_admin        = Column(Boolean, default=False,  nullable=False)
    is_active       = Column(Boolean, default=True,   nullable=False)
    ssh_public_key  = Column(String(1024), nullable=True)  # authorized_keys for SSH
    # Optional Jupyter password the user chose EXPLICITLY.  When set it acts as
    # a second factor: the proxy stops vouching for them and Jupyter's own login
    # form must be satisfied.  Most users never set this.
    hashed_jupyter_password = Column(String(255), nullable=True)

    # ── Credentials derived from the account password ────────────────────
    # The account password is stored as bcrypt, which cannot be converted into
    # the formats Jupyter and sshd need.  These are derived at the moments the
    # plaintext is legitimately in hand (create / change / reset / login) so the
    # user has ONE password everywhere and configures nothing.  Neither is
    # reversible, and neither is ever shown.
    account_jupyter_hash = Column(String(255), nullable=True)  # argon2, Jupyter format
    unix_password_hash   = Column(String(255), nullable=True)  # sha512-crypt, /etc/shadow

    # SHA-256 of the token the `submit` / `queue` / `cancel` commands use.  The
    # token itself is high-entropy and written into the user's workspace, so a
    # plain digest is enough to look it up without storing anything reusable.
    job_token_hash  = Column(String(64), unique=True, index=True, nullable=True)

    # Bumped whenever credentials or access are revoked; JWTs carry the value
    # they were minted with, so raising it invalidates every outstanding token
    # for this user immediately (deactivation, password reset, forced logout).
    token_version   = Column(Integer, default=0, nullable=False)

    # ── Quotas (None = platform default) ──────────────────────────────────
    # Disk budget for jupyter_data/<user>; sessions refuse to start above it.
    disk_quota_mb   = Column(Integer, nullable=True)
    # GPU-hour budget per budget period (QUOTA_PERIOD, a week by default);
    # 0/None = unlimited.  Charged as wall-clock x GPUs held.
    gpu_hours_quota = Column(Float, nullable=True)
    # CPU core-hour budget for the same period; 0/None = unlimited.  Charged
    # as wall-clock x cores allocated, to workspaces and jobs alike.  This is
    # the CPU budget the platform enforces; GpuAssignment.
    # cpu_limit_seconds below is an RLIMIT_CPU ceiling the old process backend
    # applies to one process tree, which is a different thing entirely.
    cpu_hours_quota = Column(Float, nullable=True)
    # Preferred Jupyter image (validated against the platform allow-list).
    preferred_image = Column(String(255), nullable=True)

    # An existing directory on the host to use as this user's workspace,
    # instead of the platform creating one under JUPYTER_DATA_DIR.  Set by an
    # administrator, never inferred from the username: deciding that the
    # platform account "ubuntu" is the same person as the host account
    # "ubuntu" is a judgement only a human can make, and getting it wrong
    # hands that host account to whoever registered the name.
    home_path       = Column(String(512), nullable=True)

    created_at      = Column(DateTime, default=datetime.utcnow, nullable=False)

    # ── Trash ─────────────────────────────────────────────────────────────
    # Deleting a user does not remove the row: the account is marked here and
    # kept, so the username stays reserved (nobody can register it and inherit
    # the previous person's files) and the usage/job history it owns stays
    # intact for statistics.  An administrator restores it or deletes it for
    # good from the trash.
    deleted_at         = Column(DateTime, nullable=True, index=True)
    # Name of the ``.deleted-<user>-<timestamp>`` directory the workspace was
    # renamed to, so a restore renames exactly that one back and a purge knows
    # what to remove.  None for a user whose workspace is a real home
    # directory the platform must not touch.
    archived_workspace = Column(String(255), nullable=True)
    # JSON snapshot of state that lives in other tables and would otherwise be
    # lost while the user sits in the trash (currently the GPU assignment).
    # Taken at delete, replayed at restore.
    restore_state      = Column(Text, nullable=True)

    @property
    def is_deleted(self) -> bool:
        """True while the account is in the trash."""
        return self.deleted_at is not None

    @property
    def jupyter_password_set(self) -> bool:
        """True when the user configured a personal Jupyter password."""
        return bool(self.hashed_jupyter_password)

    # Each user has at most one GPU assignment and one Jupyter session.
    gpu_assignment = relationship(
        "GpuAssignment",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )
    jupyter_session = relationship(
        "JupyterSession",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )
    usage_records = relationship(
        "UsageRecord",
        back_populates="user",
        cascade="all, delete-orphan",
    )
    jobs = relationship(
        "Job",
        back_populates="user",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} username={self.username!r}>"


class GpuAssignment(Base):
    __tablename__ = "gpu_assignments"

    id              = Column(Integer, primary_key=True, index=True)
    user_id         = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    # Comma-separated GPU indices, e.g. "0,1" or "2"
    gpu_indices       = Column(String(255), nullable=False)
    memory_limit_mb   = Column(Integer, nullable=True)
    # CPU cores the container may use (cgroup cpu.max).  This is the knob an
    # admin actually wants.  NULL = DEFAULT_CPU_CORES.
    cpu_cores         = Column(Float, nullable=True)
    # Cumulative CPU-SECONDS ceiling (RLIMIT_CPU) for one process tree.  Dead
    # since the process backend was removed: cgroups cannot express a
    # cumulative budget, so nothing reads this and the admin form does not
    # offer it.  The per-user compute budget that does work is
    # User.cpu_hours_quota.  It used to be the only CPU field in the UI, so "4"
    # meaning four cores was translated to 4/3600 → 0.25.
    cpu_limit_seconds = Column(Integer, nullable=True)
    # Most processes and threads the user may have at once (cgroup pids.max).
    # The fork-bomb ceiling, but also what a build with many parallel jobs or a
    # DataLoader with many workers runs into, so it has to be per-user rather
    # than one number for the whole platform.  NULL = CONTAINER_PIDS_LIMIT.
    max_processes     = Column(Integer, nullable=True)
    created_at      = Column(DateTime, default=datetime.utcnow,  nullable=False)
    updated_at      = Column(DateTime, default=datetime.utcnow,
                             onupdate=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="gpu_assignment")

    def __repr__(self) -> str:
        return f"<GpuAssignment id={self.id} user_id={self.user_id} gpus={self.gpu_indices!r}>"


class JupyterSession(Base):
    __tablename__ = "jupyter_sessions"

    id            = Column(Integer, primary_key=True, index=True)
    user_id       = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    port          = Column(Integer, nullable=False)
    # pid belonged to the removed process backend.  The column stays so old
    # rows still load; nothing writes it, and a row that has one is not running.
    pid           = Column(Integer, nullable=True)
    container_id  = Column(String(128), nullable=True)
    image         = Column(String(255), nullable=True)  # image actually used
    ssh_port      = Column(Integer, nullable=True)      # host port for sshd
    # Encrypted at rest (services.crypto), shown back to its owner only.
    ssh_password  = Column(String(512), nullable=True)
    status        = Column(
        Enum(SessionStatus),
        default=SessionStatus.stopped,
        nullable=False,
    )
    # Encrypted at rest (services.crypto).
    token         = Column(String(512), nullable=False)
    base_url      = Column(String(255), nullable=False)
    created_at    = Column(DateTime, default=datetime.utcnow, nullable=False)
    # Written by the proxy on real traffic, drives idle reaping.
    last_activity = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="jupyter_session")

    def __repr__(self) -> str:
        return f"<JupyterSession id={self.id} user_id={self.user_id} status={self.status}>"


class UsageRecord(Base):
    """One row per Jupyter session lifetime, the accounting ledger.

    Lets admins answer "who consumed how much GPU time this month", enforce
    :attr:`User.gpu_hours_quota`, and see historical peaks after the container
    is long gone.
    """

    __tablename__ = "usage_records"

    id            = Column(Integer, primary_key=True, index=True)
    user_id       = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    username      = Column(String(64), nullable=False)   # kept after user deletion
    gpu_indices   = Column(String(255), nullable=False, default="")
    gpu_count     = Column(Integer, default=0, nullable=False)
    # CPU cores the workload was allowed to use, so CPU time can be accounted
    # the same way GPU time is, a CPU-only job consumes real capacity and
    # should show up in the statistics and in fair-share scheduling.
    cpu_cores     = Column(Float, default=0.0, nullable=False)
    image         = Column(String(255), nullable=True)
    backend       = Column(String(16), nullable=False, default="container")
    started_at    = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    ended_at      = Column(DateTime, nullable=True)
    # Filled on stop: wall-clock seconds × number of GPUs / cores held.
    gpu_seconds   = Column(Float, default=0.0, nullable=False)
    cpu_seconds   = Column(Float, default=0.0, nullable=False)
    peak_memory_mb = Column(Integer, nullable=True)
    end_reason    = Column(String(32), nullable=True)   # user | admin | idle | oom | shutdown

    user = relationship("User", back_populates="usage_records")

    __table_args__ = (
        Index("ix_usage_user_started", "user_id", "started_at"),
    )

    def __repr__(self) -> str:
        return f"<UsageRecord user={self.username!r} gpu_seconds={self.gpu_seconds}>"


class Job(Base):
    """A batch job: a shell script the platform runs for a user on a GPU.

    Submitting does not require owning a GPU.  The scheduler holds the job in a
    queue and starts it when a GPU has room for what it asked for, which is how
    users without an assignment get GPU time at all.  While it runs it is
    subject to exactly the same caps as its owner's interactive workspace.
    """

    __tablename__ = "jobs"

    id           = Column(Integer, primary_key=True, index=True)
    user_id      = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    username     = Column(String(64), nullable=False)
    name         = Column(String(128), nullable=True)

    # Both relative to the user's workspace root; never absolute, never
    # escaping it (validated on submit).
    script       = Column(String(512), nullable=False)
    workdir      = Column(String(512), nullable=False, default="")
    output_path  = Column(String(512), nullable=True)   # <workdir>/output.<id>.out

    # What the job asked for.  gpu_count = 0 means it only needs CPU.
    gpu_count       = Column(Integer, default=1, nullable=False)
    gpu_memory_mb   = Column(Integer, default=0, nullable=False)
    gpu_indices     = Column(String(64), nullable=True)   # filled at dispatch
    # Whether this job is held to its gpu_memory_mb figure.  True for
    # everything submitted since the reservation became binding; False for the
    # jobs that were already in the system when it did.  A run started under a
    # documented "this is advisory" must not be killed by the deployment that
    # changes its mind, those jobs are only flagged.
    gpu_memory_enforced = Column(Boolean, default=True, nullable=False)
    # VRAM the job was last measured holding, refreshed by the scheduler.  It
    # is stored rather than measured on demand so the dashboard can show a job
    # approaching its limit without an exec into the container per page load,
    # and so the warning arrives before the stop rather than with it.
    gpu_memory_used_mb  = Column(Integer, nullable=True)
    # The high-water mark, kept because the live figure above is whatever the
    # last reading caught and is often zero by the time a job has ended.
    gpu_memory_peak_mb  = Column(Integer, nullable=True)
    # Cores the job was actually given, recorded at dispatch: a job with no GPU
    # still occupies CPU capacity, and both the scheduler and the statistics
    # need to know how much.
    cpu_cores       = Column(Float, default=0.0, nullable=False)
    max_runtime_minutes = Column(Integer, nullable=True)
    # Seconds this job has already run in segments that have ended: a pause
    # closes one and a resume opens the next.  Without it a paused job would
    # come back with its runtime limit reset and its runtime displayed as zero,
    # and `started_at` alone cannot say how long the job has really run.
    runtime_seconds = Column(Float, default=0.0, nullable=False)

    status       = Column(Enum(JobStatus), default=JobStatus.queued, nullable=False, index=True)
    container_id = Column(String(128), nullable=True)
    exit_code    = Column(Integer, nullable=True)
    message      = Column(String(512), nullable=True)   # why it failed, if it did

    created_at   = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    started_at   = Column(DateTime, nullable=True)
    finished_at  = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="jobs")

    __table_args__ = (
        Index("ix_jobs_status_created", "status", "created_at"),
    )

    @property
    def is_active(self) -> bool:
        return self.status in (JobStatus.queued, JobStatus.starting,
                               JobStatus.running, JobStatus.paused)

    def __repr__(self) -> str:
        return f"<Job id={self.id} user={self.username!r} status={self.status}>"


class AuditLog(Base):
    """Security-relevant actions, kept so an incident can be reconstructed."""

    __tablename__ = "audit_logs"

    id         = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    actor      = Column(String(64), nullable=True)     # username or "system"
    action     = Column(String(64), nullable=False, index=True)
    target     = Column(String(128), nullable=True)
    detail     = Column(Text, nullable=True)
    ip_address = Column(String(64), nullable=True)

    def __repr__(self) -> str:
        return f"<AuditLog {self.action} actor={self.actor!r} target={self.target!r}>"
