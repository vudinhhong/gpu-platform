#!/bin/bash
# Bring the two emulated nodes of docker-compose.emu.yml up or down on the dev
# stack.  "up" asks the dev master for two join tokens through its admin API
# (from inside its backend container, so the host firewall does not matter)
# and starts the nodes with them; a node that has joined before keeps its
# credential in data/emuN/node.json and needs no new token.
set -euo pipefail
cd "$(dirname "$0")/../.."

COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.gpu.yml
         -f docker-compose.host.yml -f docker-compose.emu.yml)

token() {
    PLATFORM_EXEC=gpu-platform-dev-backend-1 ADMIN_PASSWORD_FILE=~/.gpu_dev_admin_pw \
    python3 - <<'PY'
import sys
sys.path.insert(0, "scripts/experiments")
import platform_api as api
status, body = api._request("POST", "/api/admin/nodes/join-tokens", api.admin_token(), {"site": "default"})
assert status == 201, body
print(body["token"])
PY
}

# Both nodes unless named: "up node1" brings up only emu1 (GPU 0).
SERVICES=("${@:2}")
[ ${#SERVICES[@]} -gt 0 ] || SERVICES=(node1 node2)

case "${1:-}" in
    up)
        for svc in "${SERVICES[@]}"; do
            n=${svc#node}
            mkdir -p "data/emu$n"
            [ -f "data/emu$n/node.json" ] || export "EMU${n}_JOIN_TOKEN=$(token)"
            # The node's own HAMi-core: the master's build, copied as a real
            # node would build it; run/ (per-job files) stays the node's.
            # Copied as root, since the node's data directory is partly root's.
            if [ -f data/hami/libvgpu.so ]; then
                docker run --rm --entrypoint sh -v "$PWD/data:/d" gpu-platform-dev-backend -c \
                    "mkdir -p /d/emu$n/hami && cd /d/hami && cp -p libvgpu.so build-info.json /d/emu$n/hami/ \
                     && { [ ! -f verified.json ] || cp -p verified.json /d/emu$n/hami/; }"
            fi
            export "EMU${n}_HAMI_HOST_DIR=$PWD/data/emu$n/hami"
        done
        "${COMPOSE[@]}" up -d "${SERVICES[@]}"
        ;;
    down)
        "${COMPOSE[@]}" stop "${SERVICES[@]}"
        "${COMPOSE[@]}" rm -f "${SERVICES[@]}"
        ;;
    *)
        echo "usage: $0 up|down [node1] [node2]" >&2
        exit 2
        ;;
esac
