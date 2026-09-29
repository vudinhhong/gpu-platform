#!/bin/bash
# Entrypoint for per-user Jupyter containers (GPU Platform).
#
# NOTE: bash, not sh.  The Jupyter auth arguments are built as a bash array.
# Under dash (Ubuntu's /bin/sh) `AUTH_ARGS=(...)` is a syntax error and the
# container dies before Jupyter ever starts.
#
# Starts:
#   1. GPU keeper (background loop touching the NVIDIA driver)
#   2. sshd (background, per-user account with the platform-provided
#      password / public key), only when SSH_ENABLED=1
#   3. JupyterLab (foreground, container PID 1)
#
# Persistent layout (survives container recreation):
#   $PLATFORM_HOME            ← bind: the user's workspace, their HOME
#                               *and* the directory JupyterLab serves, so SSH
#                               and the notebook browser show the same files.
#                               Dotfiles (.local for `pip install --user`,
#                               .ssh, .jupyter, .cache) live inside it and are
#                               hidden from the file browser.
#   /etc/ssh/host_keys        ← bind: jupyter_data/<user>/.ssh_host_keys/
#
# Configuration arrives via environment variables set by the platform backend:
#   JUPYTER_TOKEN, JUPYTER_BASE_URL, CUDA_VISIBLE_DEVICES, GPU_KEEPER_INTERVAL,
#   SSH_ENABLED, SSH_USER_NAME, SSH_USER_PASSWORD, SSH_PUBLIC_KEY

set -e

echo "[entrypoint] CUDA_VISIBLE_DEVICES=${CUDA_VISIBLE_DEVICES:-<unset>}"
echo "[entrypoint] base_url=${JUPYTER_BASE_URL:-/}"

# ── GPU keeper ──────────────────────────────────────────────────────────────
sh /usr/local/bin/gpu_keeper.sh >/proc/1/fd/1 2>&1 &

# ── Account ─────────────────────────────────────────────────────────────────
# Same sanitisation as services/container_manager.sanitize_username, keep in
# sync or the /home bind mount will not match.
SSH_NAME="${SSH_USER_NAME:-jupyter}"
SAFE_NAME=$(echo "$SSH_NAME" | tr 'A-Z' 'a-z' | sed 's/[^a-z0-9_-]/_/g' | cut -c1-28)
RUN_UID="${SSH_USER_UID:-1000}"
RUN_GID="${SSH_USER_GID:-$RUN_UID}"
# HOME is the workspace itself: one directory for SSH and JupyterLab.  The
# platform mounts it at the path it has on the machine outside.  A mapped home
# keeps its own path so a conda installation inside it still finds itself, and
# anything else gets /home/<name>.  The fallback is where workspaces used to
# be mounted, for a container created before the move.
USER_HOME="${PLATFORM_HOME:-/workspace}"

# The account is created with the uid and gid that ALREADY own the workspace.
# When the workspace is a directory that existed before the platform, someone's
# own home on this machine, rewriting its ownership would take their files
# away from them outside the container, so the account adapts to the files
# rather than the other way round.
if ! getent group "$RUN_GID" >/dev/null 2>&1; then
    groupadd --gid "$RUN_GID" "$SAFE_NAME" 2>/dev/null || true
fi
GROUP_NAME=$(getent group "$RUN_GID" | cut -d: -f1)
GROUP_NAME="${GROUP_NAME:-$SAFE_NAME}"

if ! id "$SAFE_NAME" >/dev/null 2>&1; then
    if ! useradd --no-create-home --home-dir "$USER_HOME" --shell /bin/bash \
            --uid "$RUN_UID" --gid "$GROUP_NAME" "$SAFE_NAME" 2>/dev/null; then
        # uid collision (restart with a different name), so fall back to autos
        useradd --no-create-home --home-dir "$USER_HOME" --shell /bin/bash "$SAFE_NAME"
    fi
else
    usermod --home "$USER_HOME" "$SAFE_NAME" 2>/dev/null || true
fi

# The home directory is a bind mount from the host.  Only the top level and the
# dotfile directories are chowned here: the workspace can hold hundreds of
# gigabytes of datasets, and a recursive chown at every start would make
# launching a session take minutes.  The backend does a one-time deep repair.
# Created only when absent, so a workspace that already existed keeps whatever
# its owner arranged.
mkdir -p "$USER_HOME/.cache" "$USER_HOME/.local" 2>/dev/null || true
if [ ! -e "$USER_HOME/.bashrc" ]; then
    if [ -f /etc/skel/.bashrc ]; then
        cp /etc/skel/.bashrc /etc/skel/.profile "$USER_HOME/" 2>/dev/null || true
    else
        # Minimal but usable: colour, history that survives, and completion.
        cat > "$USER_HOME/.bashrc" <<'BASHRC'
case $- in *i*) ;; *) return;; esac
HISTSIZE=5000; HISTFILESIZE=20000; HISTCONTROL=ignoreboth; shopt -s histappend checkwinsize
PS1='\[\033[01;32m\]\u@\h\[\033[00m\]:\[\033[01;34m\]\w\[\033[00m\]\$ '
alias ls='ls --color=auto'; alias ll='ls -alF'; alias grep='grep --color=auto'
BASHRC
    fi
fi
# Only ever the dotfile directories, and only when they are not already owned:
# a mapped home belongs to its owner and is not the platform's to rewrite.
for d in "$USER_HOME/.cache" "$USER_HOME/.local"; do
    [ -d "$d" ] && [ "$(stat -c %u "$d" 2>/dev/null)" != "$RUN_UID" ] && \
        chown -R "$RUN_UID:$RUN_GID" "$d" 2>/dev/null || true
done
for dotfile in "$USER_HOME/.bashrc" "$USER_HOME/.profile"; do
    [ -e "$dotfile" ] && [ "$(stat -c %u "$dotfile" 2>/dev/null)" != "$RUN_UID" ] && \
        chown "$RUN_UID:$RUN_GID" "$dotfile" 2>/dev/null || true
done
# The platform's own directory is always ours to arrange.
[ -d /platform ] && chown "$RUN_UID:$RUN_GID" /platform 2>/dev/null || true

# Workspaces used to be mounted at /workspace for everyone, and anything
# installed while that was true wrote the path into itself: a conda prefix
# appears in .bashrc, in every wrapper script's shebang and in conda-meta.
# Moving the mount without this would break exactly what moving it was meant
# to fix.  The symlink keeps the old path resolving to the same files.
# rmdir only succeeds on an empty directory, so a real /workspace is never
# touched.
if [ "$USER_HOME" != "/workspace" ]; then
    [ -d /workspace ] && [ ! -L /workspace ] && rmdir /workspace 2>/dev/null
    [ -e /workspace ] || ln -s "$USER_HOME" /workspace
fi

# ── Batch job mode ──────────────────────────────────────────────────────────
# Everything above (the account, its home, its ownership) is what a job needs
# too, so it shares this entrypoint rather than duplicating it. The difference
# starts here: run one script instead of serving JupyterLab.
if [ -n "${PLATFORM_JOB_SCRIPT:-}" ]; then
    JOB_DIR="$USER_HOME/${PLATFORM_JOB_WORKDIR:-.}"
    JOB_OUT="$USER_HOME/${PLATFORM_JOB_OUTPUT:-output.out}"
    echo "[job] ${PLATFORM_JOB_ID:-?}: ${PLATFORM_JOB_SCRIPT} in ${JOB_DIR}"

    # The redirection happens inside the user's own shell so the output file
    # belongs to them, and `bash -l` loads their profile, the same environment
    # they get over SSH, including anything they pip-installed into .local.
    exec gosu "$SAFE_NAME" env \
        HOME="$USER_HOME" \
        USER="$SAFE_NAME" \
        PATH="$USER_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" \
        PIP_USER=1 \
        bash -lc '
            cd "$1" || exit 127
            exec bash "$HOME/$2" > "$3" 2>&1
        ' _ "$JOB_DIR" "$PLATFORM_JOB_SCRIPT" "$JOB_OUT"
fi

# ── Login banner: the real limits, because df/free/nproc cannot show them ──
cat > /etc/profile.d/99-platform-limits.sh <<'BANNER'
# Interactive logins only; keep scp/rsync and command-mode ssh output clean.
case $- in *i*) [ -x /usr/local/bin/platform-limits ] && /usr/local/bin/platform-limits ;; esac
BANNER
chmod 644 /etc/profile.d/99-platform-limits.sh

# ── SSH sidecar ─────────────────────────────────────────────────────────────
if [ "${SSH_ENABLED:-0}" = "1" ] && command -v sshd >/dev/null 2>&1; then
    # Persistent host keys: stable SSH fingerprints across container restarts.
    HOSTKEY_DIR=/etc/ssh/host_keys
    mkdir -p "$HOSTKEY_DIR"
    for alg in rsa ecdsa ed25519; do
        key="$HOSTKEY_DIR/ssh_host_${alg}_key"
        if [ ! -f "$key" ]; then
            ssh-keygen -t "$alg" -N '' -f "$key" >/dev/null 2>&1
        fi
    done

    # Password auth.  Preferred form is a pre-hashed /etc/shadow digest of the
    # user's PLATFORM ACCOUNT password (chpasswd -e), so they log in over SSH
    # with the password they already know and no plaintext ever reaches this
    # container.  SSH_USER_PASSWORD is the legacy per-session fallback, used
    # only for accounts that have no derived credential yet.
    if [ -n "${SSH_USER_PASSWORD_HASH:-}" ]; then
        echo "$SAFE_NAME:${SSH_USER_PASSWORD_HASH}" | chpasswd -e
    elif [ -n "${SSH_USER_PASSWORD:-}" ]; then
        echo "$SAFE_NAME:${SSH_USER_PASSWORD}" | chpasswd
    else
        # No password at all: key-only access.  Lock the password field so the
        # account cannot be entered with an empty one.
        passwd -l "$SAFE_NAME" >/dev/null 2>&1 || true
    fi

    # Key-only deployments turn password auth off entirely (SSH_PASSWORD_AUTH=0).
    if [ "${SSH_PASSWORD_AUTH:-1}" = "0" ]; then
        echo "PasswordAuthentication no" > /etc/ssh/sshd_config.d/60-no-password.conf
    else
        rm -f /etc/ssh/sshd_config.d/60-no-password.conf
    fi

    # Public-key auth (key registered by the user on the platform).
    # The backend also writes this file directly into the bind-mounted home
    # whenever the key changes, so a running session picks it up without a
    # restart; this branch covers a fresh container.  An EMPTY value must
    # remove the file, otherwise a key the user deleted on the dashboard
    # would keep working forever out of the persistent volume.
    # Written to /platform/authorized_keys, which sshd reads IN ADDITION to
    # ~/.ssh/authorized_keys (see sshd_tail.conf).  A workspace may be
    # somebody's real home; truncating their authorized_keys would take away
    # their access to this machine.
    if [ -n "${SSH_PUBLIC_KEY:-}" ]; then
        mkdir -p /platform
        echo "$SSH_PUBLIC_KEY" > /platform/authorized_keys
        chown "$RUN_UID:$RUN_GID" /platform/authorized_keys
        chmod 600 /platform/authorized_keys
    else
        rm -f /platform/authorized_keys
    fi


    echo "[entrypoint] starting sshd as user '$SAFE_NAME'"
    /usr/sbin/sshd -h "$HOSTKEY_DIR/ssh_host_rsa_key" \
                   -h "$HOSTKEY_DIR/ssh_host_ecdsa_key" \
                   -h "$HOSTKEY_DIR/ssh_host_ed25519_key"
else
    echo "[entrypoint] SSH disabled, starting without sshd"
fi

# ── JupyterLab (foreground) ─────────────────────────────────────────────────
# Runs as the user account with HOME == the workspace, so `pip install --user`
# packages, .jupyter config and .cache persist alongside their notebooks, and a
# file created over SSH is the same file JupyterLab lists.
# Authentication comes in two modes:
#   * JUPYTER_PASSWORD set → password login (Jupyter issues a session cookie;
#     no secret appears in any URL).  Preferred mode.
#   * otherwise            → legacy per-session ``?token=`` mode.
cd "$USER_HOME"
JUPYTER_CONFIG_DIR="$USER_HOME/.jupyter"
if [ -n "${JUPYTER_PASSWORD:-}" ] && [ "${JUPYTER_PASSWORD_REQUIRED:-0}" = "1" ]; then
    # The user chose their own Jupyter password: a genuine second factor, so
    # the token is disabled and the platform proxy does not bypass the form.
    echo "[entrypoint] Jupyter auth: password required (user-set)"
    mkdir -p "$JUPYTER_CONFIG_DIR"
    cat > "$JUPYTER_CONFIG_DIR/jupyter_server_config.json" <<EOF
{
  "IdentityProvider": { "hashed_password": "${JUPYTER_PASSWORD}" },
  "ServerApp": { "token": "", "password_required": true }
}
EOF
    chown -R "$SAFE_NAME:$SAFE_NAME" "$JUPYTER_CONFIG_DIR" 2>/dev/null || true
    AUTH_ARGS=(--ServerApp.token="" --ServerApp.password_required=True)
elif [ -n "${JUPYTER_PASSWORD:-}" ]; then
    # Password derived from the platform account: BOTH credentials are valid.
    # The proxy presents the token so an already-authenticated user is never
    # asked to type anything, and the login form still accepts their account
    # password if they ever reach it directly.
    echo "[entrypoint] Jupyter auth: token + account password"
    mkdir -p "$JUPYTER_CONFIG_DIR"
    # The backend writes this file whenever the password changes and is the
    # source of truth; only generate it here when it is genuinely absent, so a
    # restart never resurrects the hash this container was created with.
    if [ ! -s "$JUPYTER_CONFIG_DIR/jupyter_server_config.json" ]; then
        cat > "$JUPYTER_CONFIG_DIR/jupyter_server_config.json" <<EOF
{
  "IdentityProvider": { "hashed_password": "${JUPYTER_PASSWORD}" },
  "ServerApp": { "password_required": false }
}
EOF
    fi
    chown -R "$SAFE_NAME:$SAFE_NAME" "$JUPYTER_CONFIG_DIR" 2>/dev/null || true
    AUTH_ARGS=(--ServerApp.token="${JUPYTER_TOKEN}" --ServerApp.password_required=False)
else
    echo "[entrypoint] Jupyter auth: token only"
    rm -f "$JUPYTER_CONFIG_DIR/jupyter_server_config.json" 2>/dev/null || true
    AUTH_ARGS=(--ServerApp.token="${JUPYTER_TOKEN}")
fi
exec gosu "$SAFE_NAME" env HOME="$USER_HOME" USER="$SAFE_NAME" \
    jupyter lab \
    --ip=0.0.0.0 \
    --port=8888 \
    --no-browser \
    "${AUTH_ARGS[@]}" \
    --ServerApp.base_url="${JUPYTER_BASE_URL:-/}" \
    --ServerApp.allow_origin='*' \
    --ServerApp.allow_remote_access=True \
    --ServerApp.open_browser=False
