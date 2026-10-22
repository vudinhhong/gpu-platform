#!/usr/bin/env bash
# Follow-up to mps_limit.sh: is the MPS limit per client process, and does it
# leave room for the CUDA context?  Same private-daemon set-up.
set -u
export CUDA_MPS_PIPE_DIRECTORY=/tmp/mps-pipe CUDA_MPS_LOG_DIRECTORY=/tmp/mps-log
mkdir -p $CUDA_MPS_PIPE_DIRECTORY $CUDA_MPS_LOG_DIRECTORY
nvidia-cuda-mps-control -d; sleep 1
export CUDA_MPS_PINNED_DEVICE_MEM_LIMIT="0=2G"
cat > /tmp/hold.py <<'PY'
import sys, time, torch
gib = float(sys.argv[1])
try:
    x = torch.empty(int(gib * 2**30), dtype=torch.uint8, device="cuda"); torch.cuda.synchronize()
    print(f"RESULT {sys.argv[2]} holds {gib} GiB: allowed", flush=True)
    time.sleep(float(sys.argv[3]))
except RuntimeError:
    print(f"RESULT {sys.argv[2]} holds {gib} GiB: refused", flush=True)
PY
# largest single allocation that fits under the 2 GiB limit
for g in 1.5 1.75 1.9; do python /tmp/hold.py $g "single" 0; done
# two clients at once, 1.5 GiB each: per-process limit => both allowed
python /tmp/hold.py 1.5 "client-1" 8 & sleep 3
python /tmp/hold.py 1.5 "client-2" 0
wait
echo quit | nvidia-cuda-mps-control
