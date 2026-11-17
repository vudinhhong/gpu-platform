#!/bin/bash
# Rebuild dev images from the working tree and drop the old ones:
#
#   bash scripts/experiments/rebuild_dev.sh [SERVICE...]     # default: backend
#
# Run from the runner, so that no experiment is using the stack meanwhile.
set -e
cd "$(dirname "$0")/../.."
F="-f docker-compose.yml -f docker-compose.gpu.yml -f docker-compose.host.yml"
for svc in "${@:-backend}"; do
    image="gpu-platform-dev-$svc"
    old=$(docker image inspect -f '{{.Id}}' "$image" 2>/dev/null || true)
    docker compose $F build "$svc"
    docker compose $F up -d "$svc"
    new=$(docker image inspect -f '{{.Id}}' "$image")
    if [ -n "$old" ] && [ "$old" != "$new" ]; then
        docker rmi "$old" || true
    fi
done
