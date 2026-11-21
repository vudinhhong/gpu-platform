#!/usr/bin/env python3
"""How long N processes take to initialise CUDA under a HAMi-core build.

HAMi-core issue 382: every process waits on a node-wide lock, about half a
second each, while the library looks up its host PID.  Before pull request 389
this happened in cuInit; since then it happens on first use of the GPU, so
processes that only initialise the driver (compiler children, tools) no
longer queue.  This starts N processes at once in one container mounted as the
platform mounts a job and reports the wall time, twice: each process calling
only cuInit ("init"), and also creating a context ("context").  It runs once
without the library and once with each build given.

    EXP_GPUS=1 python3 scripts/hami/startup_check.py --n 16 \\
        data/hami/libvgpu.so /path/to/older/libvgpu.so
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROBE = r'''
import ctypes, json, multiprocessing as mp, sys, time

def one(context):
    c = ctypes.CDLL("libcuda.so.1")
    dev, ctx = ctypes.c_int(), ctypes.c_void_p()
    t0 = time.perf_counter()
    assert c.cuInit(0) == 0
    if context:
        assert c.cuDeviceGet(ctypes.byref(dev), 0) == 0
        assert c.cuDevicePrimaryCtxRetain(ctypes.byref(ctx), dev) == 0
    return time.perf_counter() - t0

if __name__ == "__main__":
    n, context = int(sys.argv[1]), sys.argv[2] == "context"
    t0 = time.perf_counter()
    with mp.get_context("spawn").Pool(n) as pool:
        each = pool.map(one, [context] * n)
    print("RESULT " + json.dumps({"wall_s": round(time.perf_counter() - t0, 3),
                                  "max_init_s": round(max(each), 3)}))
'''


def run_once(lib, mode, gpu, n, image, work):
    cmd = ["docker", "run", "--rm", "--user", "65534:65534", "--network", "none",
           "--gpus", f"device={gpu}", "--entrypoint", "python3",
           "-v", f"{work}/probe.py:/probe.py:ro"]
    if lib:
        (work / "usage.cache").write_text("")
        os.chmod(work / "usage.cache", 0o666)
        cmd += ["-v", f"{lib}:/usr/local/vgpu/libvgpu.so:ro",
                "-v", f"{work}/ld.so.preload:/etc/ld.so.preload:ro",
                "-v", f"{work}/overrideEnv:/overrideEnv:ro",
                "-v", f"{work}/usage.cache:/run/vgpu/usage.cache:rw"]
    p = subprocess.run(cmd + [image, "/probe.py", str(n), mode], capture_output=True, text=True,
                       timeout=600)
    line = next((x for x in p.stdout.splitlines() if x.startswith("RESULT ")), None)
    return json.loads(line[7:]) if line else {"error": (p.stderr or p.stdout)[-400:]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("libs", nargs="+")
    ap.add_argument("--n", type=int, default=16)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--image", default="gpu-jupyter:latest")
    args = ap.parse_args()
    gpu = (os.environ.get("EXP_GPUS") or "0").split(",")[0]
    work = Path(tempfile.mkdtemp(prefix="startup-"))
    os.chmod(work, 0o755)
    (work / "probe.py").write_text(PROBE)
    (work / "ld.so.preload").write_text("/usr/local/vgpu/libvgpu.so\n")
    (work / "overrideEnv").write_text("\n".join(
        ["CUDA_DEVICE_MEMORY_LIMIT=4096m"] +
        [f"CUDA_DEVICE_MEMORY_LIMIT_{i}=4096m" for i in range(16)] +
        ["CUDA_DEVICE_MEMORY_SHARED_CACHE=/run/vgpu/usage.cache",
         "CUDA_REDIRECT=/usr/local/vgpu/libvgpu.so",
         "CUDA_DEVICE_SM_LIMIT=0", "GPU_CORE_UTILIZATION_POLICY=disable"]) + "\n")
    for f in work.iterdir():
        os.chmod(f, 0o644)
    out = {"gpu": gpu, "n": args.n, "image": args.image, "runs": {}}
    for mode in ("init", "context"):
        out["runs"][mode] = {}
        for lib in [None] + [str(Path(x).resolve()) for x in args.libs]:
            key = lib or "none"
            out["runs"][mode][key] = [run_once(lib, mode, gpu, args.n, args.image, work)
                                      for _ in range(args.reps)]
            print(json.dumps({mode: {key: out["runs"][mode][key]}}), flush=True)
    for f in work.iterdir():
        f.unlink()
    work.rmdir()
    dest = os.environ.get("EXP_OUT")
    if dest:
        Path(dest, "startup_check.json").write_text(json.dumps(out, indent=1))
    sys.exit(0)


if __name__ == "__main__":
    main()
