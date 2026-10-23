#!/usr/bin/env bash
# Run a command in a sandbox that denies every outbound IP connection.
# Unix-domain sockets (asyncio's self-pipe, pseudo-terminals) stay
# allowed, and child processes inherit the sandbox.
set -euo pipefail
exec sandbox-exec -p '(version 1) (allow default) (deny network-outbound (remote ip "*:*"))' "$@"
