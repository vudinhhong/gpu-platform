#!/usr/bin/env python3
"""How much VRAM a process holds before it has allocated anything of its own.

The VRAM guard's flat grace (JOB_GPU_OVERRUN_GRACE_MB, 1 GiB) exists because
nvidia-smi counts the CUDA context and the libraries' workspaces against a
job. This measures that overhead for PyTorch, in fresh processes, at four
stages: a bare context, after a cuBLAS matmul, after a cuDNN convolution, and
after both, each with 16 MiB of tensors the job did ask for.

Run in a CUDA container on one GPU (it allocates well under 1 GiB):

    docker run --rm --gpus device=0 -v "$PWD/scripts/experiments:/exp:ro" \\
        pytorch/pytorch:2.4.1-cuda12.1-cudnn9-runtime \\
        python /exp/cuda_context.py --reps 10 > paper/data/cuda_context.json
"""

import argparse
import json
import os
import statistics
import subprocess
import sys

STAGES = {
    "context": "x = torch.zeros(4 * 1024 * 1024, device='cuda')",
    "cublas": "x = torch.zeros(4 * 1024 * 1024, device='cuda'); a = torch.randn(1024, 1024, device='cuda'); (a @ a).sum().item()",
    "cudnn": "x = torch.zeros(4 * 1024 * 1024, device='cuda'); c = torch.nn.Conv2d(3, 16, 3).cuda(); c(torch.randn(8, 3, 64, 64, device='cuda')).sum().item()",
    "both": "x = torch.zeros(4 * 1024 * 1024, device='cuda'); a = torch.randn(1024, 1024, device='cuda'); (a @ a).sum().item(); c = torch.nn.Conv2d(3, 16, 3).cuda(); c(torch.randn(8, 3, 64, 64, device='cuda')).sum().item()",
}

CHILD = r'''
import os, subprocess, sys, torch
{stage}
torch.cuda.synchronize()
out = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_memory",
                      "--format=csv,noheader,nounits"], capture_output=True, text=True).stdout
mine = [int(l.split(",")[1]) for l in out.strip().splitlines() if l.split(",")[0].strip() == str(os.getpid())]
print(mine[0] if mine else -1)
print(torch.cuda.memory_allocated() // 2**20, torch.cuda.memory_reserved() // 2**20)
'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=10)
    args = ap.parse_args()
    import torch

    result = {"torch": torch.__version__, "cuda": torch.version.cuda,
              "cudnn": torch.backends.cudnn.version(),
              "gpu": torch.cuda.get_device_name(0),
              "module_loading": os.environ.get("CUDA_MODULE_LOADING", "(default: LAZY)"),
              "stages": {}}
    for name, code in STAGES.items():
        held, allocated, reserved = [], [], []
        for _ in range(args.reps):
            out = subprocess.run([sys.executable, "-c", CHILD.format(stage=code)],
                                 capture_output=True, text=True, check=True).stdout.split()
            held.append(int(out[0]))
            allocated.append(int(out[1]))
            reserved.append(int(out[2]))
        result["stages"][name] = {
            "nvidia_smi_mib": {"min": min(held), "median": statistics.median(held), "max": max(held)},
            "torch_allocated_mib": statistics.median(allocated),
            "torch_reserved_mib": statistics.median(reserved),
            "overhead_mib": statistics.median(h - r for h, r in zip(held, reserved)),
        }
        print(name, result["stages"][name], file=sys.stderr, flush=True)
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
