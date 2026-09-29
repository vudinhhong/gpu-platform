#!/bin/sh
# GPU keeper: stops the NVIDIA driver inside this container idling out.
#
# nvidia-smi touches the driver every couple of minutes; failures (device
# vanished, driver re-initialising) are logged but never fatal.  Started as a
# sibling of the JupyterLab process by docker-entrypoint.sh.

set -u

INTERVAL="${GPU_KEEPER_INTERVAL:-120}"

echo "[gpu-keeper] starting (interval ${INTERVAL}s)"
while true; do
    sleep "$INTERVAL"
    if command -v nvidia-smi >/dev/null 2>&1; then
        nvidia-smi --query-gpu=index,utilization.gpu --format=csv,noheader >/dev/null 2>&1 \
            && echo "[gpu-keeper] driver ok $(date +%H:%M:%S)" \
            || echo "[gpu-keeper] WARNING: nvidia-smi failed $(date +%H:%M:%S)"
    fi
done
