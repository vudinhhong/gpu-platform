# Results behind the paper

The JSON files here are the measurements and simulations reported in the
paper, as the scripts in the directory above wrote them; each records when it
was produced. No file names a person: the deployment's four accounts appear as
user1..user4, in order of first activity, in `trace.json` and
`deployment.json` alike, and experiment accounts are `expa`, `expb`, `expc`
and `expw`.

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

The simulations can be re-run from `trace.json` alone, for example

    python3 scripts/experiments/fairshare_sim.py replay \
        --trace scripts/experiments/results/trace.json --out replay.json

The measurements need the platform running on a GPU host.
