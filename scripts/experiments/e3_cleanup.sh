#!/bin/bash
# After E3 is aborted: remove its Slurm container and the dev jobs and
# workspaces of the E3 accounts, then the usual dev cleanup.
cd "$(dirname "$0")/../.."
docker ps -aq --filter label=exp.runner=e3 | xargs -r docker rm -f >/dev/null
docker ps -a --filter label=gpu-platform-dev.role=jupyter --format '{{.ID}} {{.Label "gpu-platform-dev.user"}}' \
    | awk '$2 ~ /^e3/ {print $1}' | xargs -r docker rm -f >/dev/null
bash scripts/experiments/dev_cleanup.sh
