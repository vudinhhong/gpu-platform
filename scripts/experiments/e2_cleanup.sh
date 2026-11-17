#!/bin/bash
# After E2 is aborted: remove the dev workspaces of the experiment accounts
# (the backend marks their sessions stopped when it restarts), then the
# usual dev cleanup.  Production containers are labelled gpu-platform.* and
# do not match.
cd "$(dirname "$0")/../.."
for u in $(seq -f "exp%02g" 1 16); do
    docker ps -aq --filter label=gpu-platform-dev.role=jupyter \
        --filter label=gpu-platform-dev.user="$u" | xargs -r docker rm -f >/dev/null
done
bash scripts/experiments/dev_cleanup.sh
