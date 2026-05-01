#!/bin/sh
# Minimal entrypoint for the triage agent container.
#
# Intentionally skips the default warp-agent entrypoint's git/gh setup
# to maintain the isolation boundary: the agent inside the container
# has no GitHub credentials and does not mutate the repo. All GitHub
# interactions happen from the Python driver on the host.

set -e

# Resolve the stable oz binary that ships in the base image. The warp-agent
# image lays this down at a stable path that matches the pr-description
# reference pattern.
AGENT_BIN_DIR="/opt/warpdotdev/oz-stable"
AGENT_BINARY="$AGENT_BIN_DIR/oz"

export PATH="$PATH:$AGENT_BIN_DIR"

if [ -z "$WARP_API_KEY" ]; then
    echo "WARP_API_KEY is not set" >&2
    exit 1
fi

exec "$AGENT_BINARY" "$@"
