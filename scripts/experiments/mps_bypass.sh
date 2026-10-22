#!/usr/bin/env bash
# Can a client that does not cooperate escape an MPS memory limit?
# The limit is set on the daemon (server side) this time, not in the client's
# environment.  Then a client asks for 6 GiB three ways:
#   cooperating   uses the daemon's pipe directory
#   env-override  same, but sets its own larger limit in the environment
#   no-mps        points CUDA_MPS_PIPE_DIRECTORY somewhere else, so no daemon
set -u
export CUDA_MPS_PIPE_DIRECTORY=/tmp/mps-pipe CUDA_MPS_LOG_DIRECTORY=/tmp/mps-log
mkdir -p $CUDA_MPS_PIPE_DIRECTORY $CUDA_MPS_LOG_DIRECTORY /tmp/elsewhere
nvidia-cuda-mps-control -d; sleep 1
echo "set_default_device_pinned_mem_limit 0 2G" | nvidia-cuda-mps-control
TRY='import torch,sys
try:
    x=torch.empty(6*2**30,dtype=torch.uint8,device="cuda"); torch.cuda.synchronize(); print("RESULT",sys.argv[1],"6 GiB: allowed")
except RuntimeError: print("RESULT",sys.argv[1],"6 GiB: refused")'
python -c "$TRY" cooperating
CUDA_MPS_PINNED_DEVICE_MEM_LIMIT="0=40G" python -c "$TRY" env-override
CUDA_MPS_PIPE_DIRECTORY=/tmp/elsewhere python -c "$TRY" no-mps
echo quit | nvidia-cuda-mps-control
