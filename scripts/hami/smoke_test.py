#!/usr/bin/env python3
"""Does the HAMi-core build hold a container to its limit, and only to it?

Runs one throwaway container on one GPU, mounted exactly as
container_manager.hami_kwargs() mounts a job (library, read-only
/etc/ld.so.preload and /overrideEnv, writable usage region), as a non-root
user, and checks:

  within      an allocation under the limit succeeds
  over        one that would pass the limit fails with CUDA out-of-memory
  env         the user setting CUDA_DEVICE_MEMORY_LIMIT higher changes nothing
  unset       unsetting LD_PRELOAD changes nothing (the loader reads ld.so.preload)
  two_procs   two processes in the container share one limit
  meminfo     cudaMemGetInfo reports the limit as the card's size

    EXP_GPUS=1 EXP_OUT=/tmp/out python3 scripts/hami/smoke_test.py
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HAMI = ROOT / "data" / "hami"
IMAGE = os.environ.get("SMOKE_IMAGE", "pytorch/pytorch:2.4.1-cuda12.1-cudnn9-runtime")
LIMIT_MIB = int(os.environ.get("SMOKE_LIMIT_MIB", 4096))

PROBE = r'''
import json, os, subprocess, sys, torch
MiB = 2**20
def alloc(mib):
    try:
        t = torch.empty(mib * MiB, dtype=torch.uint8, device="cuda"); torch.cuda.synchronize()
        return True, t
    except torch.OutOfMemoryError as e:
        return False, str(e).splitlines()[0][:160]
r = {}
free, total = torch.cuda.mem_get_info()
r["meminfo_total_mib"] = total // MiB
ok, a = alloc(int(sys.argv[1]) // 2)
r["within"] = ok
ok, b = alloc(int(sys.argv[1]))
r["over"] = "refused" if not ok else "ALLOWED"
r["over_msg"] = b if not ok else None
if sys.argv[2] == "child":
    # A second process allocating while this one holds half the limit.
    out = subprocess.run([sys.executable, "-c",
        "import torch; t=torch.empty(int(%s)*2**20//2+512*2**20, dtype=torch.uint8, device='cuda'); print('ALLOWED')"
        % sys.argv[1]], capture_output=True, text=True)
    r["two_procs"] = "ALLOWED" if "ALLOWED" in out.stdout else "refused"
print("RESULT " + json.dumps(r))
'''


def main():
    gpu = (os.environ.get("EXP_GPUS") or "1").split(",")[0]
    out = Path(os.environ.get("EXP_OUT") or tempfile.mkdtemp())
    info = json.loads((HAMI / "build-info.json").read_text())

    run = Path(tempfile.mkdtemp(prefix="hami-smoke-", dir=HAMI))
    os.chmod(run, 0o755)
    env_lines = [f"CUDA_DEVICE_MEMORY_LIMIT={LIMIT_MIB}m"] + \
        [f"CUDA_DEVICE_MEMORY_LIMIT_{i}={LIMIT_MIB}m" for i in range(16)] + \
        ["CUDA_DEVICE_MEMORY_SHARED_CACHE=/run/vgpu/usage.cache",
         "CUDA_REDIRECT=/usr/local/vgpu/libvgpu.so", "CUDA_DEVICE_SM_LIMIT=0",
         "GPU_CORE_UTILIZATION_POLICY=disable"]
    (run / "overrideEnv").write_text("\n".join(env_lines) + "\n")
    (run / "ld.so.preload").write_text("/usr/local/vgpu/libvgpu.so\n")
    (run / "usage.cache").write_text("")
    for f, m in (("overrideEnv", 0o644), ("ld.so.preload", 0o644), ("usage.cache", 0o666)):
        os.chmod(run / f, m)

    probe = run / "probe.py"
    probe.write_text(PROBE)
    os.chmod(probe, 0o644)

    def docker(extra_env, args, with_hami=True):
        cmd = ["docker", "run", "--rm", "--label", "exp.runner=hami-smoke", "--user", "1000:1000",
               "--gpus", f"device={gpu}", "-v", f"{probe}:/probe.py:ro"]
        if with_hami:
            cmd += ["-v", f"{HAMI}/libvgpu.so:/usr/local/vgpu/libvgpu.so:ro",
                    "-v", f"{run}/ld.so.preload:/etc/ld.so.preload:ro",
                    "-v", f"{run}/overrideEnv:/overrideEnv:ro",
                    "-v", f"{run}/usage.cache:/run/vgpu/usage.cache:rw"]
        for k, v in extra_env.items():
            cmd += ["-e", f"{k}={v}"]
        cmd += [IMAGE, "python", "/probe.py", *args]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        line = next((l for l in p.stdout.splitlines() if l.startswith("RESULT ")), None)
        return (json.loads(line[7:]) if line else {"error": (p.stderr or p.stdout)[-800:]})

    results = {
        "build": info, "gpu": gpu, "limit_mib": LIMIT_MIB, "image": IMAGE,
        "without_hami": docker({}, [str(LIMIT_MIB), "single"], with_hami=False),
        "with_hami": docker({}, [str(LIMIT_MIB), "single"]),
        "user_raises_env": docker({"CUDA_DEVICE_MEMORY_LIMIT": "40g",
                                   "CUDA_DEVICE_MEMORY_LIMIT_0": "40g"}, [str(LIMIT_MIB), "single"]),
        "ld_preload_unset": docker({"LD_PRELOAD": ""}, [str(LIMIT_MIB), "single"]),
        "two_processes": docker({}, [str(LIMIT_MIB), "child"]),
    }
    (run / "usage.cache").write_text("")
    verdict = {
        "within": results["with_hami"].get("within") is True,
        "over_refused": results["with_hami"].get("over") == "refused",
        "env_cannot_raise": results["user_raises_env"].get("over") == "refused",
        "ld_preload_irrelevant": results["ld_preload_unset"].get("over") == "refused",
        "shared_between_processes": results["two_processes"].get("two_procs") == "refused",
        "meminfo_shows_limit": results["with_hami"].get("meminfo_total_mib") == LIMIT_MIB,
        "baseline_not_limited": results["without_hami"].get("over") == "ALLOWED",
    }
    results["verdict"] = verdict
    (out / "smoke.json").write_text(json.dumps(results, indent=1))
    print(json.dumps(verdict, indent=1))
    for p in run.iterdir():
        p.unlink()
    run.rmdir()
    sys.exit(0 if all(verdict.values()) else 1)


if __name__ == "__main__":
    main()
