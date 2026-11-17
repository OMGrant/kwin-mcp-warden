#!/usr/bin/env bash
# kwin-mcp-warden's boundary tests as another Linux user (deferred item 12): inside a
# container built on upstream's E2E image (Debian), as its user "tester", which rootless
# podman maps to a host user id that isn't yours. The container gets no host display,
# bus, input device or home; only the image's own nested KWin.
#   tests/warden/container.sh [pytest args]
set -euo pipefail
cd "$(dirname "$0")/../.."
engine="$(command -v podman || command -v docker)"
"$engine" image exists kwin-mcp-e2e 2>/dev/null || scripts/run-e2e-docker.sh -- tests/unit/test_render.py
"$engine" build -q -f docker/warden.Dockerfile -t kwin-mcp-warden-boundary . >/dev/null
echo "host user: $(id -u); container user: tester (uid 1000 inside, a subordinate id outside)"
exec "$engine" run --rm --network none kwin-mcp-warden-boundary \
    /opt/kwin-mcp-venv/bin/python -m pytest -q -p no:cacheprovider \
    /home/tester/boundary/warden/test_boundaries.py "$@"
