#!/usr/bin/env bash
# Does NVIDIA MPS hold a client to a per-process device-memory limit on this card?
#
# Runs entirely inside one container: the MPS control daemon gets a private pipe
# directory, so only the client started here talks to it and nothing else on
# the GPU is affected.  The client is limited to 2 GiB and asks for 1, 3 and
# 6 GiB in turn.
#
#   GPU=$(nvidia-smi --query-gpu=uuid --format=csv,noheader -i 0)
#   docker run --rm --gpus "device=$GPU" -v "$PWD/scripts/experiments:/exp:ro" \
#       pytorch/pytorch:2.4.1-cuda12.1-cudnn9-runtime bash /exp/mps_limit.sh
set -u
export CUDA_MPS_PIPE_DIRECTORY=/tmp/mps-pipe CUDA_MPS_LOG_DIRECTORY=/tmp/mps-log
mkdir -p $CUDA_MPS_PIPE_DIRECTORY $CUDA_MPS_LOG_DIRECTORY
command -v nvidia-cuda-mps-control >/dev/null || { echo "RESULT mps-control not available in container"; exit 0; }
nvidia-cuda-mps-control -d || { echo "RESULT could not start MPS daemon"; exit 0; }
sleep 1
CUDA_MPS_PINNED_DEVICE_MEM_LIMIT="0=2G" python - <<'PY'
import torch
print("RESULT device", torch.cuda.get_device_name(0))
for gib in (1, 3, 6):
    try:
        x = torch.empty(gib * 2**30, dtype=torch.uint8, device="cuda")
        torch.cuda.synchronize()
        print(f"RESULT alloc {gib} GiB under a 2 GiB MPS limit: allowed")
        del x; torch.cuda.empty_cache()
    except RuntimeError as e:
        print(f"RESULT alloc {gib} GiB under a 2 GiB MPS limit: refused ({str(e).splitlines()[0][:90]})")
PY
echo quit | nvidia-cuda-mps-control
grep -h -i -E "error|limit|pinned" $CUDA_MPS_LOG_DIRECTORY/*.log 2>/dev/null | tail -5 | sed 's/^/LOG /'
