# Results behind the paper

The JSON files here are the measurements and simulations reported in the
paper, as the scripts in the directory above wrote them; each records when it
was produced. No file names a person: the deployment's four accounts appear as
user1..user4, in order of first activity, in `trace.json` and
`deployment.json` alike, and experiment accounts are `expa`, `expb`, `expc`
and `expw`, and those of E2 and E3 are named after the experiment.

| File | Written by | Contents |
|---|---|---|
| `trace.json` | `fairshare_sim.py export-trace` | the deployment's 796 started batch jobs and 43 workspace sessions up to 21 October 2026: submit time and real wait (seconds from the first event), duration, GPUs, requested and held VRAM, cores |
| `deployment.json` | `deployment_stats.py --anonymise` | users, jobs, sessions, waits, GPU-hours |
| `vram_jobs.json` | `vram_stats.py` | requested against held VRAM per finished GPU job |
| `isolation.json` | `isolation.py` | a tenant under four exhaustion workloads of its neighbour |
| `proxy.json` | `proxy_latency.py` | notebook proxy overhead on three paths |
| `backend_overhead.json` | `backend_overhead.py` | backend CPU and memory, one `nvidia-smi` |
| `scale.json` | `scale_bench.py` | scheduling pass and queue API against users and jobs |
| `cuda_context*.json` | `cuda_context.py` | VRAM a CUDA context holds, lazy and eager loading |
| `mps.json` | `mps_*.sh` | MPS memory limits on the reference GPUs |
| `sim_*.json` | `fairshare_sim.py` | fair-share and request-size simulations |
| `workloads.json` | `workloads.py` | nvcc, R and multi-GPU jobs through the queue |
| `e1.json` | `e1_summary.py` | E1/E1b: GPU memory overruns under measurement, HAMi-core and both |
| `e2.json` | `e2_summary.py` | E2: 1 to 16 users starting workspaces and submitting jobs at once |
| `e3.json` | `e3_summary.py` | E3: fair share and FIFO on the platform, Slurm on the same jobs, the simulator |
| `e4.json` | `e4_summary.py` | E4: placement, outages and latency with emulated nodes |
| `workspace_gpu_use.json` | `workspace_gpu_sampler.py summary` | E5: how much of the time GPUs assigned to workspaces were in use |
| `e6.json` | `e6_container.sh` | E6: time and data downloaded to set up the platform and Slurm with JupyterHub |

The simulations can be re-run from `trace.json` alone, for example

    python3 scripts/experiments/fairshare_sim.py replay \
        --trace scripts/experiments/results/trace.json --out replay.json

The measurements need the platform running on a GPU host. E1 to E4 ran on a
second installation by `runner/runner.py`, which starts an experiment only
while no user's job needs the GPUs; the raw runs stay in `runs/` (not in git).
