#!/bin/bash
# Build HAMi-core's libvgpu.so for GPU_HARD_VRAM_LIMIT.
#
# HAMi-core (Apache-2.0, https://github.com/Project-HAMi/HAMi-core) is an
# interposition library: loaded into a CUDA process it refuses allocations
# beyond a per-device limit at the cudaMalloc, instead of the platform
# noticing afterwards.  It is built against one CUDA major version and must
# be rebuilt when the host driver changes; the build records which driver it
# was made for, and the backend refuses to use a build for another driver.
#
#   bash scripts/hami/build_hami.sh            # -> data/hami/libvgpu.so
#
# Pinned to one commit so a rebuild gives the same library.  deploy.sh rebuilds
# when this pin differs from the commit of the build in data/hami.
set -euo pipefail

HAMI_REPO="${HAMI_REPO:-https://github.com/Project-HAMi/HAMi-core.git}"
HAMI_COMMIT="${HAMI_COMMIT:-3ab81ce758aeba5f9d1f310134a6777c3fc70997}"
CUDA_IMAGE="${CUDA_IMAGE:-nvidia/cuda:12.8.1-devel-ubuntu22.04}"

cd "$(dirname "$0")/../.."
SRC=third_party/hami-core
OUT=data/hami

if [ ! -d "$SRC/.git" ]; then
    git clone -q "$HAMI_REPO" "$SRC"
fi
git -C "$SRC" fetch -q origin "$HAMI_COMMIT" 2>/dev/null || true
git -C "$SRC" checkout -q "$HAMI_COMMIT"

DRIVER="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)"
mkdir -p "$OUT"

# HAMi-core's own build.sh, with its options unchanged.  It builds the Debug
# configuration, and that is not a detail: its Release configuration adds
# -fvisibility=hidden, which hides every CUDA hook, and the library then
# loads, reads its limit and enforces nothing.  (A Release build made here
# first did exactly that; the check below and scripts/hami/verify.py exist
# because of it.)  Only the library target is made: some of its tests are
# written for CUDA 13 headers and do not compile against 12.8.
docker run --rm -v "$PWD/$SRC:/src:ro" -v "$PWD/$OUT:/out" "$CUDA_IMAGE" bash -euc '
    apt-get update -qq && apt-get install -y -qq cmake >/dev/null
    cp -r /src /build && cd /build
    grep -qx "make -j\$J" build.sh || { echo "build.sh changed upstream" >&2; exit 1; }
    sed -i "s/^make -j\$J\$/make -j\$J vgpu/" build.sh
    ./build.sh >/dev/null
    cp build/libvgpu.so /out/libvgpu.so
    chown '"$(id -u):$(id -g)"' /out/libvgpu.so
'

# The hooks must be exported, or nothing is intercepted.  (Not "nm | grep -q":
# grep stops at the first match, nm dies of SIGPIPE, and pipefail reports a
# symbol that is there as missing.)
SYMS="$(nm -D --defined-only "$OUT/libvgpu.so")"
for sym in cuMemAlloc_v2 cuMemGetInfo_v2 cuGetProcAddress_v2 nvmlDeviceGetMemoryInfo; do
    if ! grep -q " T $sym\$" <<<"$SYMS"; then
        echo "ERROR: $OUT/libvgpu.so does not export $sym; it would enforce nothing." >&2
        rm -f "$OUT/libvgpu.so" "$OUT/verified.json"
        exit 1
    fi
done

sha="$(sha256sum "$OUT/libvgpu.so" | cut -d" " -f1)"
cat > "$OUT/build-info.json" <<EOF
{"hami_commit": "$HAMI_COMMIT", "cuda_image": "$CUDA_IMAGE",
 "driver": "$DRIVER", "sha256": "$sha",
 "built": "$(date -Is)"}
EOF
chmod 0644 "$OUT/libvgpu.so" "$OUT/build-info.json"
# A new build is unverified until scripts/hami/verify.py says otherwise.
rm -f "$OUT/verified.json"
echo "Built $OUT/libvgpu.so for driver $DRIVER (HAMi-core ${HAMI_COMMIT:0:12})"
# The CUDA development image is ~9 GB and only needed again after a driver
# change; deploy.sh asks for it to go.
if [ "${REMOVE_BUILD_IMAGE:-0}" = "1" ]; then
    docker rmi "$CUDA_IMAGE" >/dev/null 2>&1 || true
fi
