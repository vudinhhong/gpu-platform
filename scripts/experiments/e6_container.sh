#!/bin/bash
# Run E6 for one stack in a throw-away system container on this host:
#
#   bash scripts/experiments/e6_container.sh platform|slurm
#
# The container is a fresh Ubuntu 24.04 with systemd and its own network, and
# its own Docker and containerd data directories, bind-mounted from a
# temporary directory: overlayfs cannot be mounted on overlayfs.  E6 measures
# the set-up only, so no GPU is given and both stacks install CPU-only.
# Nothing is published on the host's ports.  Everything is removed at the
# end, results excepted (EXP_OUT, else results/runs/e6-<stack>/<time>).
# The label is not exp.runner, whose cleanup removes every such container.
# E6_APT_MIRROR (e.g. http://mirror.bizflycloud.vn) replaces the Ubuntu
# archive in the container before anything is timed: from some networks
# archive.ubuntu.com runs at tens of kB/s, which would time the mirror rather
# than the stack.  Both stacks should be run with the same setting.
set -euo pipefail
STACK="$1"
cd "$(dirname "$0")/../.."
ROOT="$PWD"
OUT="${EXP_OUT:-$PWD/scripts/experiments/results/runs/e6-$STACK/$(date +%Y%m%dT%H%M%S)}"
mkdir -p "$OUT"
NAME="e6-$STACK"
TMP="$HOME/e6tmp/$STACK"

cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    # The inner Docker data belongs to root; remove it from a container.
    docker run --rm -v "$HOME/e6tmp:/d" ubuntu:24.04 rm -rf "/d/$STACK" >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup

docker build -q -t gpu-exp-e6 scripts/experiments/e6 >/dev/null
docker network inspect e6net >/dev/null 2>&1 \
    || docker network create --subnet 10.213.7.0/24 e6net >/dev/null
mkdir -p "$TMP/work"
cp scripts/experiments/e6_setup.sh "$TMP/work/"
if [ "$STACK" = platform ]; then
    tar -czf "$TMP/work/gpu-platform-src.tar.gz" --transform 's,^\.,gpu-platform,' \
        --exclude=./.env --exclude=./.git --exclude=./data --exclude=./jupyter_data \
        --exclude=./paper --exclude=./paper_ccpe --exclude=./scripts/experiments/results \
        --exclude=./scripts/experiments/runner/state --exclude=./backend/data \
        --exclude='__pycache__' --exclude='*.pyc' --exclude=node_modules .
fi

docker run -d --name "$NAME" --hostname "e6$STACK" --privileged --cgroupns=private \
    --network e6net --label exp.e6="$STACK" \
    --tmpfs /run --tmpfs /run/lock \
    -v "$TMP/docker:/var/lib/docker" -v "$TMP/containerd:/var/lib/containerd" \
    -v "$TMP/work:/root/e6" gpu-exp-e6 >/dev/null
for _ in $(seq 60); do
    state=$(docker exec "$NAME" systemctl is-system-running 2>/dev/null || true)
    case "$state" in running|degraded) break ;; esac
    sleep 2
done
echo "system container: $state"
if [ -n "${E6_APT_MIRROR:-}" ]; then
    docker exec "$NAME" sed -i \
        -e "s,http://archive.ubuntu.com/ubuntu,$E6_APT_MIRROR/ubuntu,g" \
        -e "s,http://security.ubuntu.com/ubuntu,$E6_APT_MIRROR/ubuntu,g" \
        /etc/apt/sources.list.d/ubuntu.sources
fi
echo "${E6_APT_MIRROR:-archive.ubuntu.com}" > "$OUT/apt_mirror.txt"

args=()
[ "$STACK" = platform ] && args=(--src /root/e6/gpu-platform-src.tar.gz)
rc=0
docker exec -w /root/e6 "$NAME" bash e6_setup.sh "$STACK" "${args[@]}" --yes || rc=$?
cp "$TMP"/work/e6_"$STACK"_*.json "$TMP"/work/e6_"$STACK"_*.tar.gz "$OUT/" 2>/dev/null || true
exit $rc
