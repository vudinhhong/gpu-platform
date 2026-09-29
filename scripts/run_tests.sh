#!/usr/bin/env bash
# Run the unit / API suite.
#
#   scripts/run_tests.sh              # in a throwaway container (no setup)
#   scripts/run_tests.sh -k quota     # any pytest argument is passed through
#   scripts/run_tests.sh --local      # in the current shell, needs the deps
#
# The default path builds nothing and installs nothing into the running stack.
# It copies backend/ into a container started from the backend image, installs
# pytest there, and throws the container away afterwards, so the production
# container is never touched and a failed run leaves nothing behind.
set -euo pipefail

cd "$(dirname "$0")/.."
BACKEND="$PWD/backend"
IMAGE="${TEST_IMAGE:-workspace-gpu-backend}"

if [ "${1:-}" = "--local" ]; then
    shift
    cd "$BACKEND"
    exec python -m pytest test_api.py "$@"
fi

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "No image '$IMAGE'.  Build the stack first (./deploy.sh), or set" >&2
    echo "TEST_IMAGE to an image that has the backend's dependencies." >&2
    exit 1
fi

# Read-only bind plus a copy: the suite writes a scratch database, and the
# source tree should not collect .pyc files owned by root.
exec docker run --rm \
    -v "$BACKEND:/src:ro" \
    --entrypoint sh "$IMAGE" -c '
        set -e
        mkdir -p /tmp/run
        cp -r /src/. /tmp/run/
        cd /tmp/run
        python -m pytest --version >/dev/null 2>&1 || pip install -q pytest
        exec python -m pytest test_api.py "$@"
    ' -- "$@"
