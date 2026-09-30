#!/bin/bash
set -euo pipefail

echo "=== GPU Platform Deployment ==="

# ── Prerequisite checks ───────────────────────────────────────────────────────

command -v docker >/dev/null 2>&1 || { echo "ERROR: Docker not found. Please install Docker first."; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "ERROR: Docker Compose v2 not found. Please install the Docker Compose plugin."; exit 1; }

# ── GPU detection ────────────────────────────────────────────────────────────
# The backend container may only request NVIDIA devices when BOTH hold:
#   1. nvidia-smi works on the host
#   2. Docker has the nvidia runtime registered (nvidia-container-toolkit)

HOST_GPU=false
if command -v nvidia-smi >/dev/null 2>&1 \
   && nvidia-smi -L >/dev/null 2>&1 \
   && docker info 2>/dev/null | grep -qi 'nvidia'; then
    HOST_GPU=true
fi

if [ "$HOST_GPU" = "true" ]; then
    echo "GPU check:"
    nvidia-smi --query-gpu=index,name,memory.total --format=csv,noheader 2>/dev/null \
        | awk '{print "  GPU detected: " $0}' || true
else
    echo "GPU check: no usable NVIDIA GPU + nvidia container runtime found."
    echo "  → backend runs WITHOUT device requests.  Workspaces are still one"
    echo "    container per user (RAM, CPU, PIDs and I/O limits, own filesystem,"
    echo "    own SSH endpoint); the only thing missing is the GPU."
fi

# ── Mapped home directories ──────────────────────────────────────────────────
# A user can be pointed at a home directory that predates the platform.  For
# that to work the backend must see the same path the Docker daemon will mount,
# which is what docker-compose.homes.yml arranges (read-only).
#
# Leaving the file out does not fail loudly.  Every mapping simply reports that
# the directory is not visible, so the decision is made here rather than left
# to whoever remembers the flag.

HOME_MOUNT_ROOT="${HOME_MOUNT_ROOT:-}"
if [ -z "$HOME_MOUNT_ROOT" ] && [ -f .env ]; then
    HOME_MOUNT_ROOT="$(grep -E '^HOME_MOUNT_ROOT=' .env | tail -1 | cut -d= -f2- || true)"
fi
HOME_MOUNT_ROOT="${HOME_MOUNT_ROOT:-/home}"

# Docker Desktop (macOS, Windows, and its Linux build) runs the daemon inside a
# VM and can only bind-mount host paths that were explicitly shared with it.
# On macOS /home exists as an empty autofs mount point, so the directory test
# below passes and `up -d` then dies with "Mounts denied: the path /home is not
# shared from the host": the entire stack refuses to start over a feature that
# nobody on a laptop is using.
DOCKER_DESKTOP=false
if docker info --format '{{.OperatingSystem}}' 2>/dev/null | grep -qi 'docker desktop'; then
    DOCKER_DESKTOP=true
fi

MAP_HOMES=false
if [ "$DOCKER_DESKTOP" = "true" ] && [ "${FORCE_HOME_MOUNT:-}" != "1" ]; then
    echo "Home mapping: skipped, Docker Desktop only mounts paths shared with its VM."
    echo "  (share $HOME_MOUNT_ROOT under Settings → Resources → File sharing, then"
    echo "   rerun as FORCE_HOME_MOUNT=1 ./deploy.sh to map it anyway.)"
elif [ -d "$HOME_MOUNT_ROOT" ]; then
    MAP_HOMES=true
    echo "Home mapping: $HOME_MOUNT_ROOT mounted read-only into the backend."
else
    echo "Home mapping: $HOME_MOUNT_ROOT does not exist, mapping existing home"
    echo "  directories to users will report them as not visible."
fi

# ── HTTP entry point ─────────────────────────────────────────────────────────
# host = something already serves :80/:443 on this machine (nginx, Caddy, …).
#        The stack publishes only WEB_BIND:WEB_PORT and stays out of the way.
# edge = the bundled nginx container owns :80/:443.
#
# Honour an explicit HTTP_MODE (env or .env); otherwise pick by probing :80.

detect_http_mode() {
    if ss -ltn 2>/dev/null | grep -qE '(^|[^0-9.]):80\s'; then
        echo host
    else
        echo edge
    fi
}

HTTP_MODE="${HTTP_MODE:-}"
if [ -z "$HTTP_MODE" ] && [ -f .env ]; then
    HTTP_MODE="$(grep -E '^HTTP_MODE=' .env | tail -1 | cut -d= -f2- || true)"
fi
if [ -z "$HTTP_MODE" ]; then
    HTTP_MODE="$(detect_http_mode)"
    echo ""
    echo "HTTP mode auto-detected: $HTTP_MODE"
    [ "$HTTP_MODE" = "host" ] && echo "  (port 80 is already in use, not starting the edge nginx container)"
fi

WEB_PORT="${WEB_PORT:-7080}"
WEB_BIND="${WEB_BIND:-127.0.0.1}"

# ── Directory setup ───────────────────────────────────────────────────────────

echo ""
echo "Creating required directories..."
mkdir -p data jupyter_data nginx/ssl
echo "  data/         ✓"
echo "  jupyter_data/ ✓"
echo "  nginx/ssl/    ✓"

# ── Compose file set ──────────────────────────────────────────────────────────

COMPOSE_ARGS=(-f docker-compose.yml)
[ "$HOST_GPU" = "true" ] && COMPOSE_ARGS+=(-f docker-compose.gpu.yml)
[ "$MAP_HOMES" = "true" ] && COMPOSE_ARGS+=(-f docker-compose.homes.yml)
if [ "$HTTP_MODE" = "host" ]; then
    COMPOSE_ARGS+=(-f docker-compose.host.yml)
else
    COMPOSE_ARGS+=(--profile edge)
fi

# ── Backend GPU device nodes ─────────────────────────────────────────────────
# The NVIDIA runtime hook grants a container access to /dev/nvidia* without
# telling the daemon, so Docker never records those devices and systemd never
# hears about them.  A `systemctl daemon-reload`, which an ordinary apt upgrade
# is enough to cause, then reapplies the unit's device policy and revokes the
# GPU from a container that is still running: the host stays fine, the device
# nodes stay in place, and everything inside fails with "Failed to initialize
# NVML: Unknown Error".  Naming the nodes here puts them in the container's own
# spec, where systemd can see them and reapply them instead.
#
# Generated rather than committed: which nodes exist is this host's business.
GPU_DEVICES_FILE=docker-compose.gpu.devices.yml
if [ "$HOST_GPU" = "true" ]; then
    NVIDIA_NODES=$(ls -1 /dev/nvidia* 2>/dev/null \
        | grep -E '^/dev/nvidia([0-9]+|ctl|-uvm|-uvm-tools|-modeset)$' || true)
    if [ -n "$NVIDIA_NODES" ]; then
        {
            echo "# Generated by deploy.sh from this host's /dev. Do not edit;"
            echo "# rerun ./deploy.sh after adding or removing a GPU."
            echo "services:"
            echo "  backend:"
            echo "    devices:"
            echo "$NVIDIA_NODES" | sed 's|.*|      - "&:&:rwm"|'
        } > "$GPU_DEVICES_FILE"
        COMPOSE_ARGS+=(-f "$GPU_DEVICES_FILE")
        echo "  GPU device nodes for the backend: $(echo "$NVIDIA_NODES" | tr '\n' ' ')"
    fi
fi

# ── .env ──────────────────────────────────────────────────────────────────────

set_env_var() {
    # set_env_var KEY VALUE: replace or append a line in .env
    if grep -qE "^$1=" .env; then
        sed -i.bak "s|^$1=.*|$1=$2|" .env && rm -f .env.bak
    else
        printf '%s=%s\n' "$1" "$2" >> .env
    fi
}

if [ ! -f .env ]; then
    echo ""
    echo "Generating .env file..."

    if command -v openssl >/dev/null 2>&1; then
        SECRET_KEY=$(openssl rand -hex 32)
    else
        # `head` closes the pipe, `tr` dies of SIGPIPE, and `set -o pipefail`
        # turns that into a failed deployment even though the key is fine.
        SECRET_KEY=$(tr -dc 'a-f0-9' < /dev/urandom 2>/dev/null | head -c 64 || true)
        if [ ${#SECRET_KEY} -ne 64 ]; then
            echo "ERROR: could not generate SECRET_KEY (no openssl, no /dev/urandom)." >&2
            exit 1
        fi
    fi

    # Whether this host has a GPU is recorded, not rediscovered.  The guard
    # further down refuses to deploy when .env expects GPUs and none are
    # visible, which is right for a GPU host having a bad day and wrong for a
    # machine that never had one: it would fail every deployment after the
    # first.  Stating it here answers the question once.
    if [ "$HOST_GPU" = "true" ]; then
        CPU_ONLY_MARK=false
    else
        CPU_ONLY_MARK=true
    fi

    cat > .env << ENVEOF
SECRET_KEY=$SECRET_KEY
ADMIN_PASSWORD=admin123
HTTP_MODE=$HTTP_MODE
WEB_BIND=$WEB_BIND
WEB_PORT=$WEB_PORT
GPU_COUNT=2
# true = this host is known to have no GPU, so a missing GPU is expected and
# not a fault. Cleared automatically by deploy.sh once a GPU shows up.
CPU_ONLY=$CPU_ONLY_MARK
JUPYTER_DATA_DIR=/jupyter_data
JUPYTER_DATA_HOST_DIR=$(pwd)/jupyter_data
ENVEOF

    echo "  .env created (HTTP_MODE=$HTTP_MODE)."
    [ "$CPU_ONLY_MARK" = "true" ] && echo "  CPU_ONLY=true recorded: no GPU here, workspaces are CPU containers."
    echo ""
    echo "  ⚠  Default admin password: admin123.  CHANGE IT after first login!"
else
    echo ""
    echo ".env already exists, reconciling with this host..."

    # Drop knobs from superseded deployment approaches.  SESSION_BACKEND chose
    # between one container per user and a `jupyter lab` subprocess sharing the
    # platform's own namespaces; the subprocess backend is gone, so the setting
    # has nothing left to select.  JUPYTER_PORT_* was that backend's host port
    # pool: a container's Jupyter is reached over the Docker network by name.
    if grep -qE '^SESSION_BACKEND=process' .env; then
        echo "  ⚠  SESSION_BACKEND=process is no longer a thing.  That backend ran"
        echo "     every workspace as a subprocess in one container, where each user"
        echo "     could read the others' files; it has been removed.  Workspaces"
        echo "     are containers now, and any session it started is already gone --"
        echo "     users press Start once and get a container instead."
        # deploy.sh only ever wrote `process` on a host with no GPU, so the file
        # is already telling us what the CPU_ONLY guard below would otherwise
        # stop to ask.  Carry the answer over instead of failing the upgrade on
        # a machine that has been CPU-only all along.
        if ! grep -qE '^CPU_ONLY=' .env; then
            printf 'CPU_ONLY=true\n' >> .env
            echo "     CPU_ONLY=true carried over from it."
        fi
    fi
    sed -i.bak -e '/^COMPOSE_PROFILES=/d' -e '/^SESSION_BACKEND=/d' \
               -e '/^JUPYTER_PORT_START=/d' -e '/^JUPYTER_PORT_END=/d' .env \
        && rm -f .env.bak

    # User containers bind-mount jupyter_data/<user>; the Docker daemon
    # resolves that path on the HOST, so it must be the host-side path.
    # (A stale value from another machine silently breaks every session.)
    CURRENT_DATA_DIR="$(grep -E '^JUPYTER_DATA_HOST_DIR=' .env | tail -1 | cut -d= -f2- || true)"
    if [ "$CURRENT_DATA_DIR" != "$(pwd)/jupyter_data" ]; then
        echo "  → JUPYTER_DATA_HOST_DIR: '$CURRENT_DATA_DIR' → '$(pwd)/jupyter_data'"
    fi
    set_env_var JUPYTER_DATA_HOST_DIR "$(pwd)/jupyter_data"
    set_env_var HTTP_MODE "$HTTP_MODE"
    set_env_var WEB_BIND "$WEB_BIND"
    set_env_var WEB_PORT "$WEB_PORT"

    # A host that was running GPU workspaces a minute ago and cannot be seen to
    # have a GPU now is far more likely to be a transient nvidia-smi failure than
    # a machine that lost its cards, so a missing GPU stops the deployment rather
    # than being worked around: every workspace and every job would otherwise
    # come up without the card its owner was assigned, and the platform would
    # look broken in a way that points at the wrong layer.
    #
    # A host that genuinely has no GPU is a different statement, and a perfectly
    # good thing to share: a workspace with no card attached gets
    # NVIDIA_VISIBLE_DEVICES=void and keeps memory.max, cpu.max, the PIDs cap,
    # the I/O throttles, its own filesystem and its own SSH endpoint.  So the
    # answer is recorded once as CPU_ONLY in .env instead of being asserted with
    # a flag on every deployment.

    # Said once and remembered, rather than re-asserted on every run.
    CPU_ONLY_ENV="$(grep -E '^CPU_ONLY=' .env | tail -1 | cut -d= -f2- || true)"
    if [ "${FORCE_CPU_ONLY:-}" = "1" ] && [ "$CPU_ONLY_ENV" != "true" ]; then
        set_env_var CPU_ONLY "true"
        CPU_ONLY_ENV=true
        echo "  → CPU_ONLY=true recorded (FORCE_CPU_ONLY=1); no need to repeat the flag."
    fi
    # A GPU turning up means the exemption has outlived its reason: put the
    # guard back, so the next failed nvidia-smi is caught rather than shrugged at.
    if [ "$HOST_GPU" = "true" ] && [ "$CPU_ONLY_ENV" = "true" ]; then
        set_env_var CPU_ONLY "false"
        CPU_ONLY_ENV=false
        echo "  → CPU_ONLY cleared: this host has a GPU again."
    fi

    if [ "$HOST_GPU" != "true" ] && [ "$CPU_ONLY_ENV" != "true" ]; then
        echo ""
        echo "ERROR: this host is not recorded as CPU-only, but no usable GPU was"
        echo "       detected on it.  Refusing to deploy, because the likeliest"
        echo "       cause is a temporary fault and every workspace would come up"
        echo "       without the card it is supposed to hold."
        echo ""
        echo "  Check, in this order:"
        echo "    nvidia-smi -L                      # driver alive?"
        echo "    docker info | grep -i nvidia       # runtime registered?"
        echo "    systemctl status docker"
        echo ""
        echo "  If this host really has no GPU and you mean to run CPU-only"
        echo "  workspaces, say so once.  Containers without a GPU keep every"
        echo "  other limit:"
        echo ""
        echo "    FORCE_CPU_ONLY=1 ./deploy.sh"
        echo ""
        exit 1
    fi
fi

# ── Host GPU setup (persistence mode, keep-alive, prevents GPU detach) ──────

if [ "$HOST_GPU" = "true" ]; then
    echo ""
    if [ "$(id -u)" -eq 0 ]; then
        echo "Applying host GPU keep-alive settings (persistence mode, udev rule)..."
        bash scripts/host_gpu_setup.sh || echo "WARNING: host GPU setup failed, continuing."
    else
        echo "  ⚠  Not running as root, skipping automatic host GPU setup."
        echo "     Run 'sudo bash scripts/host_gpu_setup.sh' to stop GPUs from"
        echo "     detaching from idle user containers."
    fi
fi

# ── Build the per-user Jupyter image BEFORE starting the backend ─────────────
# User containers must never build their image at session start: that would
# block the API for the whole apt/pip install.  Building here is cheap when
# cached and guarantees the image exists before anyone can press "Start".

echo ""
# `--pull` refuses to carry a stale base layer forward, and the image is only
# promoted to :latest after it has been shown to run.  Both halves were learned
# the hard way: a cached `python3.11` that died with SIGILL produced an image
# that built without complaint and whose every workspace then crash-looped, and
# because nothing here ever ran the thing it had just built, `:latest` was
# rewritten to point at it.  Read back what was applied, do not trust that it
# worked -- the same rule the platform applies to cgroup limits.
echo "Building per-user Jupyter image (gpu-jupyter:latest)..."
if docker build --pull -f backend/jupyter.Dockerfile -t gpu-jupyter:candidate backend/; then
    if docker run --rm --entrypoint sh gpu-jupyter:candidate \
            -c 'jupyter lab --version' >/dev/null 2>&1; then
        # Keep the outgoing image reachable: a rollback tag is worth nothing if
        # it points at the same thing that has just been found wanting.
        if docker image inspect gpu-jupyter:latest >/dev/null 2>&1; then
            docker tag gpu-jupyter:latest gpu-jupyter:rollback
        fi
        docker tag gpu-jupyter:candidate gpu-jupyter:latest
        echo "  gpu-jupyter:latest ✓ (jupyter starts in it; previous image kept as gpu-jupyter:rollback)"
    else
        echo "ERROR: the image built, but 'jupyter lab --version' does not run inside it." >&2
        echo "       gpu-jupyter:latest left untouched, so running workspaces keep the" >&2
        echo "       image they have. The rejected build is kept as gpu-jupyter:candidate" >&2
        echo "       for inspection:  docker run --rm -it gpu-jupyter:candidate bash" >&2
    fi
else
    echo "WARNING: image build failed, user sessions will fall back to the process backend."
fi

# ── Snapshot, so a bad deployment can be walked back ─────────────────────────
# Three things decide what the stack becomes, and all three are easy to lose:
# the database, .env, and which compose files were merged.  Keeping the last
# few costs almost nothing and turns "work out what changed by reading the
# code" into "diff against yesterday".

SNAP_DIR="data/backups"
SNAP="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$SNAP_DIR"
echo ""
echo "Snapshot before deploying (${SNAP_DIR}/*-${SNAP}.*):"
[ -f .env ] && cp .env "$SNAP_DIR/env-$SNAP.bak" && echo "  .env           ✓"
if [ -f data/gpu_platform.db ]; then
    # The platform is running while this is taken, so ask SQLite for a
    # consistent copy instead of copying a file that is being written to.
    if command -v sqlite3 >/dev/null 2>&1 \
       && sqlite3 data/gpu_platform.db ".backup '$SNAP_DIR/db-$SNAP.db'" 2>/dev/null; then
        # .backup checkpoints into the copy, so the sidecars it leaves behind
        # are empty and only serve to accumulate past the retention glob below.
        rm -f "$SNAP_DIR/db-$SNAP.db-shm" "$SNAP_DIR/db-$SNAP.db-wal"
        echo "  database       ✓ (sqlite3 .backup)"
    else
        cp data/gpu_platform.db "$SNAP_DIR/db-$SNAP.db" && echo "  database       ✓ (copy)"
    fi
fi
# The merged, fully resolved configuration: which overlays were in effect and
# what they came out as.  This is what says whether the GPU overlay was applied,
# which is exactly the question nobody could answer after a bad deploy.
if docker compose "${COMPOSE_ARGS[@]}" config > "$SNAP_DIR/compose-$SNAP.yml" 2>/dev/null; then
    echo "  compose config ✓"
else
    rm -f "$SNAP_DIR/compose-$SNAP.yml"
fi
# Keep the last 20 of each; older ones are noise, and jupyter_data is the thing
# actually worth disk space here.
for pattern in "env-*.bak" "db-*.db" "compose-*.yml"; do
    # A pattern that matches nothing is the normal case on a first deployment:
    # there is no database to snapshot yet.  `ls` then exits non-zero, and with
    # `set -o pipefail` that aborted the whole run right here -- silently,
    # because stderr is discarded, and before a single container was started.
    # The snapshot lines were the last thing printed, which is exactly what it
    # looked like: deploy.sh reporting success and then stopping.
    # shellcheck disable=SC2086
    ls -1t $SNAP_DIR/$pattern 2>/dev/null | tail -n +21 | while read -r old; do
        rm -f "$old"
    done || true
done

# ── Build and start ───────────────────────────────────────────────────────────

echo ""
echo "Starting services (${COMPOSE_ARGS[*]})..."
docker compose "${COMPOSE_ARGS[@]}" up -d --build

# ── Post-start reachability check ────────────────────────────────────────────
# A default-DROP OUTPUT policy on the host silently blocks docker-proxy from
# reaching the container, which surfaces as a 504 from the reverse proxy, the
# connection to the published port succeeds (that is docker-proxy on loopback)
# but nothing ever answers. Catch it here instead of letting it look like an
# application bug.

# In edge mode the bundled nginx owns :80 and nothing publishes WEB_PORT, so
# probing WEB_BIND:WEB_PORT there reports a failure the stack does not have.
if [ "$HTTP_MODE" = "host" ]; then
    CHECK_URL="http://${WEB_BIND}:${WEB_PORT}"
else
    CHECK_URL="http://127.0.0.1"
fi

echo ""
echo "Checking that the host can reach the stack ($CHECK_URL)..."
REACHABLE=false
for _ in $(seq 1 15); do
    if curl -sf -m 3 "${CHECK_URL}/api/health" >/dev/null 2>&1; then
        REACHABLE=true
        break
    fi
    sleep 2
done

if [ "$REACHABLE" = "true" ]; then
    echo "  $CHECK_URL ✓"
else
    BRIDGE_IF="gpu-platform0"
    echo ""
    echo "  ✗ $CHECK_URL did not answer."
    if command -v iptables >/dev/null 2>&1 \
       && iptables -S 2>/dev/null | grep -q '^-P OUTPUT DROP' \
       && ! iptables -C OUTPUT -o "$BRIDGE_IF" -j ACCEPT 2>/dev/null; then
        echo ""
        echo "  CAUSE: this host has a default-DROP OUTPUT firewall policy and no"
        echo "  rule allowing traffic out to the Docker bridge '$BRIDGE_IF'."
        echo "  docker-proxy therefore cannot reach the container, and any reverse"
        echo "  proxy in front of the stack will return 504 Gateway Timeout."
        echo ""
        echo "  FIX:  sudo bash scripts/host_firewall_setup.sh"
    else
        echo "  Check 'docker compose ps' and 'docker logs workspace-gpu-frontend-1'."
        echo "  If the host firewall has a default-DROP OUTPUT policy, run:"
        echo "      sudo bash scripts/host_firewall_setup.sh"
    fi
fi

# ── Done ──────────────────────────────────────────────────────────────────────

# `hostname -I` is GNU-only; on macOS it is an illegal option, and under
# `set -o pipefail` the failed pipeline took the final summary with it.
SERVER_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
if [ -z "$SERVER_IP" ] && command -v ipconfig >/dev/null 2>&1; then
    SERVER_IP="$(ipconfig getifaddr en0 2>/dev/null || true)"
fi
[ -z "$SERVER_IP" ] && SERVER_IP="YOUR_SERVER_IP"

echo ""
echo "============================================"
echo "  Deployment complete!"
echo "============================================"
if [ "$HTTP_MODE" = "host" ]; then
    echo "  Mode         : host web server (no edge container)"
    echo "  Stack URL    : http://${WEB_BIND}:${WEB_PORT}"
    echo ""
    echo "  If the host firewall has a default-DROP OUTPUT policy:"
    echo "    sudo bash scripts/host_firewall_setup.sh"
    echo ""
    echo "  Point your existing nginx at it:"
    echo "    sudo cp nginx/host/gpu-platform.conf /etc/nginx/sites-available/gpu-platform"
    echo "    sudo ln -sf /etc/nginx/sites-available/gpu-platform /etc/nginx/sites-enabled/"
    echo "    sudo nginx -t && sudo systemctl reload nginx"
    echo "  (edit server_name first; then run certbot for TLS)"
else
    echo "  Mode         : bundled edge nginx"
    echo "  Platform URL : http://$SERVER_IP"
fi
echo "  Default login: admin / admin123"
echo ""
echo "  IMPORTANT: change the admin password immediately after first login."
echo "============================================"
