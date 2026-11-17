#!/bin/bash
# After E4 is aborted: the emulated nodes down (their netem rules go with
# their network namespaces), their jobs and workspaces removed, and the dev
# master back on its own settings.
cd "$(dirname "$0")/../.."
for ns in gpu-platform-emu1 gpu-platform-emu2; do
    docker ps -aq --filter "label=$ns.role" | xargs -r docker rm -f >/dev/null
done
bash scripts/experiments/emu_nodes.sh down >/dev/null 2>&1
bash scripts/experiments/dev_cleanup.sh
