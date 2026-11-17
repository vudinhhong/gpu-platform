#!/usr/bin/env python3
"""Check, on this host's GPU and driver, that the HAMi-core build enforces a limit.

Run by deploy.sh after every build and whenever the driver has changed.  It
starts a short container from the workspace image, mounted the way the
platform mounts a job, and allocates through the CUDA driver API directly
(ctypes on libcuda, so no framework is needed in the image):

  * half the limit must succeed, and the card must report the limit as its size;
  * the limit again on top must fail with CUDA_ERROR_OUT_OF_MEMORY;
  * the same with CUDA_DEVICE_MEMORY_LIMIT raised in the user's environment.

On success it writes data/hami/verified.json naming the build and the driver,
and the backend applies hard limits only with a build verified for the
driver it is running on.  On failure it removes that file.  It takes a few
seconds and a few hundred MiB on the emptiest card.

    python3 scripts/hami/verify.py [--image gpu-jupyter:latest]
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HAMI = ROOT / "data" / "hami"
LIMIT_MIB = 1024

PROBE = r'''
import ctypes, json
c = ctypes.CDLL("libcuda.so.1")
b = ctypes.byref
dev, ctx = ctypes.c_int(), ctypes.c_void_p()
assert c.cuInit(0) == 0
assert c.cuDeviceGet(b(dev), 0) == 0
assert c.cuCtxCreate_v2(b(ctx), 0, dev) == 0
free, total = ctypes.c_size_t(), ctypes.c_size_t()
c.cuMemGetInfo_v2(b(free), b(total))
p, q = ctypes.c_uint64(), ctypes.c_uint64()
within = c.cuMemAlloc_v2(b(p), %(half)d << 20)
over = c.cuMemAlloc_v2(b(q), %(limit)d << 20)
print("RESULT " + json.dumps({"total_mib": total.value >> 20, "within": within, "over": over}))
'''
CUDA_ERROR_OUT_OF_MEMORY = 2


def emptiest_gpu():
    out = subprocess.run(["nvidia-smi", "--query-gpu=index,memory.free", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True, check=True).stdout
    rows = [tuple(int(x) for x in l.split(",")) for l in out.splitlines() if l.strip()]
    return max(rows, key=lambda r: r[1])[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default="gpu-jupyter:latest")
    args = ap.parse_args()

    info = json.loads((HAMI / "build-info.json").read_text())
    driver = subprocess.run(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
                            capture_output=True, text=True, check=True).stdout.split()[0]
    verified = HAMI / "verified.json"
    verified.unlink(missing_ok=True)

    run = Path(tempfile.mkdtemp(prefix="verify-", dir=HAMI))
    os.chmod(run, 0o755)
    (run / "overrideEnv").write_text("\n".join(
        [f"CUDA_DEVICE_MEMORY_LIMIT={LIMIT_MIB}m"] +
        [f"CUDA_DEVICE_MEMORY_LIMIT_{i}={LIMIT_MIB}m" for i in range(16)] +
        ["CUDA_DEVICE_MEMORY_SHARED_CACHE=/run/vgpu/usage.cache",
         "CUDA_REDIRECT=/usr/local/vgpu/libvgpu.so",
         "CUDA_DEVICE_SM_LIMIT=0", "GPU_CORE_UTILIZATION_POLICY=disable"]) + "\n")
    (run / "ld.so.preload").write_text("/usr/local/vgpu/libvgpu.so\n")
    (run / "probe.py").write_text(PROBE % {"half": LIMIT_MIB // 2, "limit": LIMIT_MIB})
    for f in ("overrideEnv", "ld.so.preload", "probe.py"):
        os.chmod(run / f, 0o644)
    # The experiment runner hands over a card it has checked is idle.
    gpu = int(os.environ["EXP_GPUS"].split(",")[0]) if os.environ.get("EXP_GPUS") else emptiest_gpu()

    def probe(env):
        (run / "usage.cache").write_text("")
        os.chmod(run / "usage.cache", 0o666)
        cmd = ["docker", "run", "--rm", "--user", "65534:65534", "--network", "none",
               "--gpus", f"device={gpu}", "--entrypoint", "python3",
               "-v", f"{HAMI}/libvgpu.so:/usr/local/vgpu/libvgpu.so:ro",
               "-v", f"{run}/ld.so.preload:/etc/ld.so.preload:ro",
               "-v", f"{run}/overrideEnv:/overrideEnv:ro",
               "-v", f"{run}/usage.cache:/run/vgpu/usage.cache:rw",
               "-v", f"{run}/probe.py:/probe.py:ro"]
        for k, v in env.items():
            cmd += ["-e", f"{k}={v}"]
        p = subprocess.run(cmd + [args.image, "/probe.py"], capture_output=True, text=True, timeout=120)
        line = next((l for l in p.stdout.splitlines() if l.startswith("RESULT ")), None)
        return json.loads(line[7:]) if line else {"error": (p.stderr or p.stdout)[-500:]}

    plain = probe({})
    raised = probe({"CUDA_DEVICE_MEMORY_LIMIT": "40g", "CUDA_DEVICE_MEMORY_LIMIT_0": "40g"})
    for f in run.iterdir():
        f.unlink()
    run.rmdir()

    ok = (plain.get("within") == 0 and plain.get("over") == CUDA_ERROR_OUT_OF_MEMORY
          and plain.get("total_mib") == LIMIT_MIB
          and raised.get("over") == CUDA_ERROR_OUT_OF_MEMORY)
    print(json.dumps({"gpu": gpu, "driver": driver, "plain": plain, "user_raised_env": raised,
                      "ok": ok}))
    if ok:
        verified.write_text(json.dumps({"sha256": info["sha256"], "driver": driver,
                                        "image": args.image,
                                        "verified": datetime.now().astimezone().isoformat(timespec="seconds")}))
        os.chmod(verified, 0o644)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
