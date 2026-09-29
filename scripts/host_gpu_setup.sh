#!/bin/bash
# =============================================================================
# Host GPU setup for the GPU Platform (user-per-container mode)
#
# Fixes the "GPU detaches from a container after a while" problem at its ROOT:
#
#   1. NVIDIA driver persistence mode ON
#      Without it, the driver tears down its state whenever the last client
#      disconnects. Re-initialisation races with container workloads and is
#      the #1 cause of "CUDA error: unknown" / dropped GPU sessions.
#
#   2. NVreg_PreserveVideoMemoryAllocations=1
#      Stops the driver from discarding VRAM allocations on suspend/eviction,
#      so long-running notebook processes keep their CUDA contexts.
#
#   3. udev rule keeping /dev/nvidia* device nodes alive
#      Some daemons remove/re-create device nodes when the last *nvidia-smi*
#      client exits; the rule keeps them stable for running containers.
#
#   4. systemd keep-alive timer (optional, can be disabled)
#      Runs `nvidia-smi` every few minutes so the driver never fully idles.
#
# Run as root on the DOCKER HOST (not inside any container):
#   sudo ./scripts/host_gpu_setup.sh
# =============================================================================
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: run as root:  sudo $0" >&2
    exit 1
fi

echo "== GPU Platform host GPU setup =="

# ── 1. Persistence mode ─────────────────────────────────────────────────────
if command -v nvidia-smi >/dev/null 2>&1; then
    echo "-- Enabling NVIDIA persistence mode"
    nvidia-smi -pm 1 || echo "   (already enabled or not supported)"
else
    echo "WARNING: nvidia-smi not found, is the NVIDIA driver installed on the host?"
fi

# ── 2. Preserve video memory allocations across suspend / eviction ─────────
MODPROBE_CONF=/etc/modprobe.d/nvidia-preserve-vidmem.conf
echo "-- Writing $MODPROBE_CONF"
cat > "$MODPROBE_CONF" <<'EOF'
# Keep VRAM allocations alive so long-running containerised GPU workloads
# never lose their CUDA contexts (GPU Platform requirement).
options nvidia NVreg_PreserveVideoMemoryAllocations=1
EOF

# ── 3. udev rule: keep device nodes alive for containers ────────────────────
UDEV_RULE=/etc/udev/rules.d/71-nvidia-keep-alive.rules
echo "-- Writing $UDEV_RULE"
cat > "$UDEV_RULE" <<'EOF'
# Keep NVIDIA device nodes present; needed so containers with mounted GPUs
# do not lose their device handles when short-lived host processes exit.
KERNEL=="nvidia", RUN+="/bin/sh -c 'mknod -m 666 /dev/nvidia c 195 0 2>/dev/null || true'"
KERNEL=="nvidiactl", RUN+="/bin/sh -c 'mknod -m 666 /dev/nvidiactl c 195 255 2>/dev/null || true'"
EOF

# ── 4. systemd keep-alive service + timer ───────────────────────────────────
KEEPALIVE_UNIT=/etc/systemd/system/nvidia-keepalive.service
KEEPALIVE_TIMER=/etc/systemd/system/nvidia-keepalive.timer
if command -v systemctl >/dev/null 2>&1; then
    echo "-- Installing systemd keep-alive unit"
    cat > "$KEEPALIVE_UNIT" <<'EOF'
[Unit]
Description=Keep NVIDIA driver initialised for the GPU Platform

[Service]
Type=oneshot
ExecStart=/usr/bin/nvidia-smi
EOF
    cat > "$KEEPALIVE_TIMER" <<'EOF'
[Unit]
Description=Touch NVIDIA driver every 5 minutes so it never idles out

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
EOF
    systemctl daemon-reload
    systemctl enable --now nvidia-keepalive.timer
    echo "   timer enabled: $(systemctl is-enabled nvidia-keepalive.timer)"
else
    echo "systemd not found, skipping keep-alive timer"
fi

# ── 5. Reload driver options if the module is safe to reload ────────────────
echo ""
echo "IMPORTANT: the modprobe option requires a driver reload to take effect."
echo "If GPUs are in use, reboot the host (safest). Otherwise run:"
echo "    rmmod nvidia_uvm nvidia_drm nvidia_modeset nvidia && modprobe nvidia"
echo ""
echo "Done."
