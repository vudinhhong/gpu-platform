# Per-user JupyterLab image for the GPU Platform (user-per-container mode).
#
# DESIGN, everything is baked at BUILD time:
#   * All Python packages (JupyterLab, pandas, ...) are installed in the
#     builder stage and copied into the runtime stage. Starting a user
#     container is instant, no apt/pip work happens at RUN time.
#   * The runtime base is plain ubuntu:22.04. NVIDIA CUDA runtime libraries
#     are NOT needed inside the image: the nvidia container runtime injects
#     the matching driver libraries at container start (device request
#     capabilities: [gpu, compute, utility]).
#   * Multi-stage keeps the image small (no pip cache, no build tools).
#
# Also includes:
#   * a GPU "keeper" loop that stops the NVIDIA driver from idling out
#   * an SSH sidecar (per-session password or registered public key)
#
# The entrypoint creates a per-user Linux account at start and bind-mounts
#   jupyter_data/<user>        → /workspace        (notebooks, data)
#   jupyter_data/<user>/.home  → /home/<user>      (pip --user libs, .ssh, …)
# so both survive container recreation.

# ── Stage 1: build the Python stack ─────────────────────────────────────────
# Pinned interpreter (3.11) must match the runtime stage's python3 version so
# compiled wheels and the ipykernel spec stay compatible.
FROM python:3.11-slim-bookworm AS pip-builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Pin the official index (some environments inherit a broken mirror via
    # pip.conf/build args) and survive flaky networks.
    PIP_INDEX_URL=https://pypi.org/simple \
    PIP_RETRIES=10 \
    PIP_DEFAULT_TIMEOUT=120

RUN python3 -m pip install --no-cache-dir --upgrade pip

# Install with --prefix so the whole stack lands in one COPY-able directory.
# --ignore-installed: deps already present in the builder image (packaging,
# six, ...) must STILL be materialised into the prefix, otherwise the runtime
# stage misses them.
# NOTE: notebook 7.1.3 requires jupyterlab >=4.1.1,<4.2, older 4.0.x pins
# make pip resolution fail (this used to break the whole image build).
RUN python3 -m pip install --no-cache-dir --ignore-installed --prefix=/opt/pypkgs \
        "jupyterlab==4.1.6" \
        "notebook==7.1.3" \
        "ipykernel==6.29.5" \
        "numpy==2.0.1" \
        "pandas==2.2.2" \
        "matplotlib==3.9.1"

# ── Stage 2: minimal runtime ────────────────────────────────────────────────
FROM ubuntu:22.04

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

# The runtime is also the shell people work in, over SSH and in JupyterLab's
# terminal, so it needs to behave like a normal machine: completion, a pager,
# an editor, and the tools installers expect to find.  procps is not optional,
# without it there is no ps, top or free at all.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.11 python3.11-venv python3-pip \
        bash-completion \
        procps \
        curl wget \
        git \
        less nano vim-tiny \
        unzip bzip2 xz-utils \
        rsync \
        openssh-server \
        gosu \
        tzdata \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3.11 /usr/bin/python3 \
    && ln -sf /usr/bin/vim.tiny /usr/bin/vim

# Interactive shells: bash (not sh), colour, and completion.  JupyterLab's
# terminal launches $SHELL and falls back to /bin/sh when it is unset, which
# is dash: a bare "$" prompt, no colours, no tab completion.
ENV SHELL=/bin/bash \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8
RUN printf '%s\n' \
    "" \
    "# --- GPU Platform defaults -------------------------------------------" \
    "# The terminals this image is used from are colour-capable; ~/.bashrc" \
    "# reads this before deciding whether to colour the prompt." \
    "force_color_prompt=yes" \
    "" \
    "# Completion, if the shell did not already load it." \
    "if ! shopt -oq posix && [ -z \"\${BASH_COMPLETION_VERSINFO:-}\" ]; then" \
    "    [ -r /usr/share/bash-completion/bash_completion ] && . /usr/share/bash-completion/bash_completion" \
    "fi" \
    "" \
    "# Anything the user installed into their workspace comes first." \
    "case \":\$PATH:\" in *\":\$HOME/.local/bin:\"*) ;; *) PATH=\"\$HOME/.local/bin:\$PATH\" ;; esac" \
    >> /etc/bash.bashrc

# Drop the pre-built Python stack in.  Debian's python3 imports from
# /usr/lib/python3/dist-packages; console scripts go to /usr/local/bin.
COPY --from=pip-builder /opt/pypkgs/lib/python3.11/site-packages /usr/lib/python3/dist-packages
COPY --from=pip-builder /opt/pypkgs/bin /usr/local/bin
# JupyterLab's built UI assets + jupyter data files live under the prefix's
# share/ dir (sys.prefix is /usr at runtime), without them /lab 404s.
COPY --from=pip-builder /opt/pypkgs/share /usr/share
# etc/jupyter/jupyter_server_config.d/*.json is how server extensions enable
# themselves.  Omitting it silently disabled ALL of them: no Terminal in the
# launcher (/api/terminals 404s), no jupyter-lsp, no notebook_shim.  It must
# land under sys.prefix, which is /usr in this runtime stage.
COPY --from=pip-builder /opt/pypkgs/etc /usr/etc
# Console-script shebangs point at the builder interpreter (/usr/local/bin/
# python3.11), satisfy them with a symlink to the runtime interpreter.
RUN ln -sf /usr/bin/python3.11 /usr/local/bin/python3.11 \
    && ln -sf python3.11 /usr/local/bin/python3

# Register the default ipykernel (uses the runtime interpreter).
RUN python3 -m ipykernel install --sys-prefix

# ── GPU keeper (anti GPU-detach) ────────────────────────────────────────────
COPY gpu_keeper.sh /usr/local/bin/gpu_keeper.sh
RUN chmod +x /usr/local/bin/gpu_keeper.sh

# ── `platform-limits`, what the cgroup actually enforces ───────────────────
# df/free/nproc/top are not cgroup-aware and report the whole host, which makes
# a correctly limited container look unlimited.  This reads the cgroup itself.
COPY platform-limits.sh /usr/local/bin/platform-limits
# Cgroup-aware replacements on PATH ahead of /usr/bin.  The originals stay put
# and are still reachable as /usr/bin/<name>; these exist because the real
# tools read host-wide /proc and /sys and therefore make a correctly limited
# container look unlimited.
COPY cgroup-tools/ /usr/local/bin/
# ── submit / queue / cancel ────────────────────────────────────────────────
# The job commands run inside the user's workspace and talk to the platform
# with the credential it leaves there, so nothing has to be typed.
COPY cli-tools/submit cli-tools/queue cli-tools/myjobs cli-tools/cancel /usr/local/bin/
COPY cli-tools/_jobclient.py /usr/local/lib/gpu-platform/_jobclient.py
RUN chmod +x /usr/local/bin/platform-limits /usr/local/bin/nproc \
        /usr/local/bin/free /usr/local/bin/df /usr/local/bin/top \
        /usr/local/bin/submit /usr/local/bin/queue /usr/local/bin/myjobs \
        /usr/local/bin/cancel \
    && ln -sf /usr/local/bin/platform-limits /usr/local/bin/limits

# ── SSH sidecar + entrypoint ────────────────────────────────────────────────
COPY sshd_tail.conf /etc/ssh/sshd_config.d/50-gpu-platform.conf
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && mkdir -p /run/sshd /etc/ssh/host_keys /workspace

EXPOSE 8888 22

# Everything (sshd, gpu-keeper, Jupyter) is launched by the entrypoint.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
