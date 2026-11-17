#!/bin/bash
# After an experiment on the dev stack is aborted: take its jobs off the GPUs
# and put the dev backend back on its own settings (no pinned card, the
# configured enforcement).  Touches only containers labelled for the dev
# instance; production's are labelled gpu-platform.* and are never matched.
cd "$(dirname "$0")/../.."
docker ps -q --filter label=gpu-platform-dev.role=job | xargs -r docker rm -f >/dev/null
docker ps -q --filter label=exp.runner | xargs -r docker rm -f >/dev/null
docker compose -f docker-compose.yml -f docker-compose.gpu.yml -f docker-compose.host.yml \
    up -d backend >/dev/null 2>&1
