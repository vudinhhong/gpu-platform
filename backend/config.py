from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Security ─────────────────────────────────────────────────────────
    # Override in production via environment variable or .env file.
    SECRET_KEY: str = "change-this-in-production-use-a-random-32-char-string"
    # Session lifetime.  Revocation no longer depends on expiry: every JWT
    # carries the user's token_version, so deactivating a user, resetting
    # their password or forcing a logout invalidates outstanding tokens at
    # once (see auth.get_current_user).
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480
    # Minimum length for any password the platform accepts.
    MIN_PASSWORD_LENGTH: int = 10
    # Mark the proxy cookie Secure.  Leave False only for plain-HTTP LAN
    # deployments; deploy.sh sets it True whenever TLS terminates upstream.
    COOKIE_SECURE: bool = False

    # Login throttling (services.ratelimit)
    LOGIN_MAX_FAILURES: int = 8
    LOGIN_FAIL_WINDOW_SECONDS: int = 300
    LOGIN_LOCKOUT_SECONDS: int = 900

    # Database – SQLite by default; swap for PostgreSQL in production
    DATABASE_URL: str = "sqlite:///./data/gpu_platform.db"

    # Base directory for per-user notebook working directories
    JUPYTER_DATA_DIR: str = "/jupyter_data"
    # Directory an administrator may map a user's workspace into, when that
    # user already has a home on this machine.  A mapping must live under this
    # root, so a typo cannot mount "/" or "/etc" into someone's shell.  The
    # same path must be visible to the backend, see docker-compose.homes.yml.
    HOME_MOUNT_ROOT: str = "/home"
    # The SAME directory as seen on the DOCKER HOST.  When the backend runs
    # inside a container and spawns user containers through docker.sock, bind
    # mounts are resolved against the host filesystem.
    JUPYTER_DATA_HOST_DIR: str = ""

    # Number of physical GPUs available on the host
    GPU_COUNT: int = 2
    # Return simulated GPUs when nvidia-smi is unavailable.  OFF by default:
    # phantom hardware on a production host let admins assign GPUs that do
    # not exist and made a broken driver look healthy.
    ALLOW_MOCK_GPU: bool = False

    # Default admin password; always change via env var in production
    ADMIN_PASSWORD: str = "admin123"

    # ── Per-user resource limits.  0 / None = unlimited. ─────────────────
    DEFAULT_MEMORY_LIMIT_MB: int = 8192
    # RLIMIT_CPU: cumulative CPU-seconds one process tree may burn before the
    # kernel kills it.  PROCESS BACKEND ONLY, cgroups have no equivalent, so
    # the container backend ignores it.  What caps a workspace there is
    # DEFAULT_CPU_CORES below; what caps a user's batch work is
    # DEFAULT_CPU_HOURS_QUOTA.
    DEFAULT_CPU_LIMIT_SECONDS: int = 0

    # ── Per-user workspace containers ─────────────────────────────────────
    # Default image for per-user Jupyter containers
    JUPYTER_IMAGE: str = "gpu-jupyter:latest"
    # Optional allow-list users may choose from: "Label=ref,Label2=ref2".
    # A request for anything outside this list falls back to JUPYTER_IMAGE, so
    # the client never gets to name an arbitrary image to run on the host.
    JUPYTER_IMAGES: str = ""
    # Docker network the backend + user containers share (see docker-compose.yml)
    DOCKER_NETWORK: str = "workspace-gpu_gpu-net"
    # Default CPU-core cap for user containers (0 = unlimited)
    DEFAULT_CPU_CORES: float = 2.0
    # cgroup pids.max per user container, fork-bomb ceiling (0 = unlimited)
    CONTAINER_PIDS_LIMIT: int = 512
    # Size of /dev/shm inside user containers.  Docker's default is 64 MB,
    # which is far too small for anything that uses shared memory between
    # processes: a PyTorch DataLoader with workers exhausts it immediately and
    # its workers die with "Connection reset by peer", which looks like a bug
    # in the user's code.  Capped at half the container's memory limit, since
    # tmpfs pages are charged to the same cgroup.
    CONTAINER_SHM_SIZE_MB: int = 2048

    # ── Per-container disk I/O limits (cgroup blkio / io.max) ────────────
    DISK_READ_BPS_MB: int = 150    # max MB/s reads per container
    DISK_WRITE_BPS_MB: int = 80    # max MB/s writes per container
    DISK_READ_IOPS: int = 0        # 0 = unlimited
    DISK_WRITE_IOPS: int = 0
    # Relative share when containers contend for the same disk (10..1000).
    BLKIO_WEIGHT: int = 500

    # ── Quotas ───────────────────────────────────────────────────────────
    # Per-user disk budget for jupyter_data/<user> (0 = unlimited).  Being over
    # it costs the GPU and the job queue, never the workspace itself: that is
    # the only place the user can delete anything from.
    DEFAULT_DISK_QUOTA_MB: int = 0
    # How long a time budget lasts before it refills: "week" (Monday 00:00) or
    # "month" (the 1st).  A month is too long to be a useful lever, somebody
    # who burns their allowance on the 3rd is locked out for four weeks, and
    # the operator who wants to give them a little more has to edit the quota
    # itself.  A week forgives a bad estimate without anyone intervening.
    QUOTA_PERIOD: str = "week"
    # Hours east of UTC for deciding when a period starts and ends.  The rest
    # of the platform works in UTC, but "your budget resets on Monday" has to
    # mean the user's Monday: at UTC+7 the reset would otherwise land at 07:00
    # local, in the middle of a working morning.
    QUOTA_TZ_OFFSET_HOURS: float = 0.0

    # ── Job time budgets, per user, per period (0 = unlimited) ───────────
    # These cover the BATCH QUEUE and nothing else.  A workspace is somebody's
    # own seat at the machine, already bounded by their assignment; the queue
    # is work left to run unattended, where one person submitting fifty jobs
    # takes the machine from everybody else.  So an interactive session costs
    # nothing here, and an idle one is IDLE_TIMEOUT_MINUTES' problem.
    #
    # GPU-hours a user's jobs may spend, counted as wall-clock x cards held.
    DEFAULT_GPU_HOURS_QUOTA: float = 0.0
    # CPU core-hours their jobs may spend, as wall-clock x cores allocated.
    # This is the CPU budget with teeth; DEFAULT_CPU_LIMIT_SECONDS above is an
    # RLIMIT_CPU ceiling per process tree that nothing applies any more.
    DEFAULT_CPU_HOURS_QUOTA: float = 0.0
    # What to do about a RUNNING JOB whose owner has just run out.  Admission
    # cannot cover this on its own: a job admitted with twenty minutes of
    # budget left would otherwise run for three days.
    #   requeue = stop it and put it back in the queue, where it waits for the
    #             budget to refill and then starts again by itself (default).
    #             The script re-runs from the beginning and its output file is
    #             rewritten, so jobs that matter should checkpoint.
    #   stop    = stop it and mark it failed; the user resubmits by hand
    #   warn    = note it on the job and let it run
    JOB_TIME_QUOTA_ACTION: str = "requeue"
    # A job holding no GPU is treated differently, because it can be frozen
    # instead: the cgroup freezer stops every process where it stands and the
    # scheduler thaws it when the budget refills, so nothing is re-run and
    # nothing is lost.  A paused container still holds its RAM, which is why
    # this is not offered for GPU jobs, where the thing being held would be a
    # whole card that nobody else could use for the rest of the period.
    #   pause = freeze it and resume it at the refill (default)
    #   (anything else) = fall back to JOB_TIME_QUOTA_ACTION above
    JOB_TIME_QUOTA_CPU_ACTION: str = "pause"
    # How often each running workspace's copy of these figures is refreshed,
    # for the `limits` command inside the container.  The budgets themselves
    # are applied by the job scheduler, on JOB_SCHEDULER_INTERVAL.
    QUOTA_REFRESH_INTERVAL_SECONDS: int = 60
    # What to do about a user who is over their disk quota while a session is
    # already running.  The start-time check cannot help there, and the
    # filesystem does not enforce the quota, so this is the only lever.
    #   warn = record it and surface it to admins (default)
    #   stop = also stop the session, freeing the GPU
    DISK_QUOTA_ACTION: str = "warn"
    # With DISK_QUOTA_ACTION="stop", how long a session that is over budget
    # is left alone before it is stopped.  Without this the stop lands
    # within a minute of every start, and the user never gets long enough
    # to delete anything, the quota becomes a lockout instead of a limit.
    DISK_QUOTA_GRACE_MINUTES: int = 30
    # `du` results are cached this long; it walks the whole data directory.
    DISK_USAGE_TTL_SECONDS: int = 300
    # How stale a disk measurement may be when it is about to cost someone the
    # GPU or the job queue.  The TTL above is tuned for a scan nobody is
    # waiting on; this one is what a user who has just deleted files waits.
    DISK_USAGE_RECHECK_SECONDS: int = 15
    DISK_USAGE_TIMEOUT_SECONDS: int = 30

    # ── Batch job queue ──────────────────────────────────────────────────
    # Master switch for job submission.
    JOBS_ENABLED: bool = True
    # How often the scheduler finalises finished jobs and dispatches queued
    # ones.  Short enough to feel responsive, long enough not to hammer
    # nvidia-smi and the Docker API.
    JOB_SCHEDULER_INTERVAL: int = 10
    # How often VRAM is sampled and the reservation enforced.  Separate from
    # the scheduler above because it is far cheaper: one nvidia-smi covers
    # every job, so the window a job can overrun in is a second, not ten.
    GPU_GUARD_INTERVAL: float = 1.0
    # Jobs one user may have running at once.  The queue exists to share GPUs;
    # without this a single user's backlog would occupy every card.
    JOB_MAX_RUNNING_PER_USER: int = 2
    # Queued jobs one user may have waiting (submission is refused beyond it).
    JOB_MAX_QUEUED_PER_USER: int = 50
    # VRAM left unclaimed on a GPU after admitting a job, so a card is never
    # filled to the last megabyte by the scheduler.
    JOB_GPU_HEADROOM_MB: int = 512
    # There is deliberately no default VRAM request.  A job that names no
    # figure is a CPU job; a job that wants a GPU has to say how much of one.
    # A guessed reservation is worse than none: it reads as a decision, and
    # the scheduler fits other work against it.
    # Hard runtime cap; a job still running after this is stopped (0 = none).
    JOB_MAX_RUNTIME_MINUTES: int = 0
    # GPU indices the queue may use; empty = every GPU on the machine.
    JOB_GPU_POOL: str = ""
    # How much of a job's output the dashboard and `queue <id>` show.  Job
    # output can be gigabytes (a training run printing every step), so only
    # the end is ever read: the byte window bounds memory regardless of file
    # size, and the line count bounds what crosses the network.
    JOB_OUTPUT_TAIL_LINES: int = 500
    JOB_OUTPUT_TAIL_BYTES: int = 128 * 1024
    # One pathological line (a progress bar with no newlines, binary data)
    # must not become the whole response.
    JOB_OUTPUT_MAX_LINE_CHARS: int = 1000
    # A progress bar written with \r is a single line, but it still grows the
    # file: tqdm at a few thousand iterations a second writes several hundred
    # megabytes an hour.  Past this the job is flagged so its owner can see
    # what is eating their disk quota.  It is NOT stopped, killing a twelve
    # hour training run over a verbose log would be worse than the log.
    # A job's --gpu-memory is a scheduling reservation, and these GPUs cannot
    # partition memory in hardware (MIG is not available on consumer cards),
    # so nothing in the driver stops a job from allocating past it.  The
    # platform enforces it instead: a job found holding more than its
    # allowance is stopped, told what it really used, and left for its owner
    # to resubmit with an honest figure.  Without that the reservation is an
    # honour system that pays to cheat, understating it jumps the queue, and
    # the job placed into the space that was never free is the one that dies.
    #
    # Allowance = max(request x FACTOR, request + GRACE_MB).  The factor
    # forgives a request that was merely low; the grace covers what the job
    # never chose, a CUDA context is several hundred MB per process before
    # the first tensor exists, and nvidia-smi counts it.
    JOB_GPU_OVERRUN_FACTOR: float = 1.1
    JOB_GPU_OVERRUN_GRACE_MB: int = 1024
    # "stop" enforces as described; "warn" only records it on the job, which
    # leaves reservations advisory.
    JOB_GPU_OVERRUN_ACTION: str = "stop"
    JOB_OUTPUT_WARN_MB: int = 512
    # A hard ceiling that DOES stop the job.  0 = never; set it only if a
    # runaway log filling the disk is the bigger risk.
    JOB_MAX_OUTPUT_MB: int = 0
    # CPU cores the queue may hand out.  0 = every core on the machine minus
    # JOB_CPU_RESERVED_CORES.  Jobs without a GPU still consume real capacity,
    # so they are admitted against this the way GPU jobs are against VRAM.
    JOB_CPU_POOL_CORES: float = 0
    # Cores kept back for the platform itself and for interactive work.
    JOB_CPU_RESERVED_CORES: float = 4
    # A job that has just started is not using its cores yet, so the measured
    # load cannot see it.  It holds its full allocation for this long, which
    # stops the scheduler from admitting a second and a third into the same
    # idle reading before the first has loaded anything.
    JOB_CPU_RAMP_SECONDS: int = 120
    # A load measurement older than this is not trusted; the scheduler falls
    # back to adding allocations up rather than admitting work on a stale
    # figure.  Metrics refresh every METRICS_INTERVAL_SECONDS.
    JOB_CPU_MEASURE_MAX_AGE_SECONDS: int = 60

    # ── Fair share ───────────────────────────────────────────────────────
    # Queue order is by how much each user has been given, not by who
    # submitted first: ten jobs from one person must not push everybody else
    # behind them.  A user's score is their recent usage (decayed) plus what
    # they are running right now; the lowest score goes first, ties by
    # submission time.
    #
    # How far back usage counts at all.
    FAIRSHARE_WINDOW_HOURS: float = 24
    # How quickly past usage stops mattering, after one half-life it weighs
    # half as much, so yesterday's heavy user is not punished forever.
    FAIRSHARE_HALFLIFE_HOURS: float = 6
    # One CPU-core-second expressed in GPU-seconds, so the two can be added.
    # A GPU here is worth roughly this many cores.
    FAIRSHARE_CPU_CORE_WEIGHT: float = 0.125
    # A running job counts as if it will keep running this long, which is what
    # makes current allocation outweigh history and produces the interleaving.
    FAIRSHARE_LOOKAHEAD_SECONDS: float = 600

    # ── Background loops ─────────────────────────────────────────────────
    # GPU keep-alive touch interval inside user containers (anti-detach)
    GPU_KEEPER_INTERVAL: int = 120
    # Self-heal scan interval for dead containers (seconds)
    SELF_HEAL_INTERVAL: int = 30
    # Resource telemetry refresh (seconds), `docker stats` is blocking, so
    # this runs in a worker thread and the API only reads the cached snapshot.
    METRICS_INTERVAL_SECONDS: int = 10
    # Stop sessions with no proxy traffic for this long (0 = never).  This is
    # the single highest-value knob on a small cluster: it returns GPUs that
    # someone forgot to release.
    IDLE_TIMEOUT_MINUTES: int = 0
    REAPER_INTERVAL_SECONDS: int = 300
    # Keep user sessions alive across a backend restart.  The old behaviour
    # (stop everything on shutdown) meant every platform update killed every
    # running notebook.
    KEEP_SESSIONS_ON_SHUTDOWN: bool = True

    # ── One password per user ────────────────────────────────────────────
    # Derive the Jupyter login and the container's UNIX password from the
    # platform account password, so a user configures nothing and never sees a
    # second or third secret.  Turning this off restores the old behaviour:
    # per-session random SSH passwords and token links for Jupyter.
    UNIFIED_PASSWORD: bool = True
    # Accept password authentication over SSH at all.  Set false for a
    # key-only deployment: the SSH port range is reachable from the internet
    # and, unlike the web login, sshd is not covered by the platform's
    # lockout, keys remove that exposure entirely.
    SSH_PASSWORD_AUTH: bool = True

    # ── Per-user SSH access (container backend only) ─────────────────────
    SSH_ENABLED: bool = True
    SSH_PORT_START: int = 2222
    SSH_PORT_END: int = 2321

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"  # compose-only knobs (HTTP_MODE, WEB_PORT…) are fine


settings = Settings()
