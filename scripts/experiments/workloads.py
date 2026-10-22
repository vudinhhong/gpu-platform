#!/usr/bin/env python3
"""Workloads beyond notebooks, run the way a user runs them.

An experiment account installs its own toolchains into its workspace (no
administrator involved, nothing added to the image) and submits batch jobs
with the ordinary ``submit`` command:

    cuda     a CUDA C program compiled with nvcc (nvcc from conda-forge, user-local)
    r        an R script (R from micromamba, user-local)
    ddp      PyTorch DistributedDataParallel across 2 GPUs with torchrun

Each job's status, placement, runtime and the last lines of its output are
recorded.  The account is deactivated afterwards; its files stay until an
administrator purges it.

    python3 scripts/experiments/workloads.py --out paper/data/workloads.json
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import platform_api as api  # noqa: E402

USER = "expw"
PASSWORD = "Experiment-pass-1"

SETUP = r'''
set -e
cd ~
mkdir -p wl && cd wl
python3 -m pip install --user -q torch==2.4.1 --extra-index-url https://download.pytorch.org/whl/cu121
if [ ! -x ~/micromamba/bin/micromamba ]; then
  mkdir -p ~/micromamba/bin
  curl -fsSL https://micro.mamba.pm/api/micromamba/linux-64/latest | tar -xj -C ~/micromamba bin/micromamba
fi
~/micromamba/bin/micromamba create -y -q -r ~/micromamba -n r -c conda-forge r-base >/dev/null
~/micromamba/bin/micromamba create -y -q -r ~/micromamba -n cuda -c conda-forge \
    cuda-nvcc=12.4 cuda-cudart-dev=12.4 >/dev/null

cat > vecadd.cu <<'CU'
#include <cstdio>
__global__ void add(const float* a, const float* b, float* c, int n) {
  int i = blockIdx.x * blockDim.x + threadIdx.x; if (i < n) c[i] = a[i] + b[i]; }
int main() {
  const int n = 1 << 24; size_t s = n * sizeof(float);
  float *a, *b, *c; cudaMallocManaged(&a, s); cudaMallocManaged(&b, s); cudaMallocManaged(&c, s);
  for (int i = 0; i < n; i++) { a[i] = 1.0f; b[i] = 2.0f; }
  add<<<(n + 255) / 256, 256>>>(a, b, c, n); cudaDeviceSynchronize();
  int bad = 0; for (int i = 0; i < n; i++) bad += c[i] != 3.0f;
  cudaDeviceProp p; int d; cudaGetDevice(&d); cudaGetDeviceProperties(&p, d);
  printf("device=%s elements=%d errors=%d\n", p.name, n, bad); return bad != 0; }
CU

cat > cuda.sh <<'SH'
set -e
~/micromamba/bin/micromamba run -r ~/micromamba -n cuda nvcc --version | tail -1
~/micromamba/bin/micromamba run -r ~/micromamba -n cuda nvcc -o vecadd vecadd.cu
./vecadd
SH

cat > fit.R <<'R'
set.seed(1); x <- rnorm(1e6); y <- 3 * x + rnorm(1e6)
m <- lm(y ~ x); cat(sprintf("R %s slope=%.4f cores=%s\n", R.version.string, coef(m)[2], parallel::detectCores()))
R
cat > r.sh <<'SH'
~/micromamba/bin/micromamba run -r ~/micromamba -n r Rscript fit.R
SH

cat > ddp.py <<'PY'
import os, torch, torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP
dist.init_process_group("nccl"); r = dist.get_rank(); torch.cuda.set_device(r)
m = DDP(torch.nn.Linear(1024, 1024).cuda(), device_ids=[r]); opt = torch.optim.SGD(m.parameters(), 0.01)
for _ in range(200):
    opt.zero_grad(); m(torch.randn(256, 1024, device="cuda")).pow(2).mean().backward(); opt.step()
w = m.module.weight.detach().clone(); dist.all_reduce(w); w /= dist.get_world_size()
print(f"rank={r} world={dist.get_world_size()} gpu={torch.cuda.get_device_name(r)} "
      f"in_sync={torch.allclose(w, m.module.weight)}", flush=True)
dist.destroy_process_group()
PY
cat > ddp.sh <<'SH'
nvidia-smi -L
python3 -m torch.distributed.run --standalone --nproc_per_node 2 ddp.py
SH
echo setup-done
'''

JOBS = {
    "cuda": "submit cuda.sh --gpu-memory 4000 --name wl-cuda",
    "r": "submit r.sh --no-gpu --name wl-r",
    "ddp": "submit ddp.sh --gpus 2 --gpu-memory 6000 --name wl-ddp",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="workloads.json")
    args = ap.parse_args()

    admin = api.admin_token()
    api.ensure_user(admin, USER, PASSWORD, gpu_indices=[], memory_limit_mb=16384,
                    cpu_cores=8, max_processes=1024)
    token = api.login(USER, PASSWORD)
    api.start_workspace(token)
    container = f"gpu-jupyter-{USER}"
    api.wait_running(container)
    time.sleep(5)

    t0 = time.time()
    out = api.sh(container, USER, SETUP, timeout=3600)
    if "setup-done" not in out.stdout:
        raise SystemExit(f"setup failed:\n{out.stdout[-2000:]}\n{out.stderr[-2000:]}")
    setup_s = time.time() - t0

    ids = {}
    for name, cmd in JOBS.items():
        res = api.sh(container, USER, f"cd ~/wl && {cmd}")
        m = re.search(r"job (\d+) submitted", res.stdout)
        if not m:
            raise SystemExit(f"{name}: {res.stdout}\n{res.stderr}")
        ids[name] = int(m.group(1))

    results = {}
    deadline = time.time() + 3600
    while time.time() < deadline and len(results) < len(ids):
        for name, jid in ids.items():
            if name in results:
                continue
            status, job = api._request("GET", f"/api/jobs/{jid}", token)
            if status == 200 and job["status"] in ("succeeded", "failed", "cancelled", "timeout"):
                tail = job.get("output_tail") or ""
                results[name] = {"id": jid, "status": job["status"], "gpus": job.get("gpu_indices"),
                                 "gpu_count": job.get("gpu_count"),
                                 "runtime_s": job.get("runtime_seconds"),
                                 "output_tail": tail.strip().splitlines()[-6:]}
                print(name, results[name], flush=True)
        time.sleep(10)

    api.stop_workspace(token)
    api.deactivate(admin, USER)
    with open(args.out, "w") as fh:
        json.dump({"measured": datetime.now().astimezone().isoformat(timespec="seconds"),
                   "setup_seconds": round(setup_s), "jobs": results}, fh, indent=1)


if __name__ == "__main__":
    main()
