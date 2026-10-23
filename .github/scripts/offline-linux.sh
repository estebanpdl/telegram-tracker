#!/usr/bin/env bash
# Run a command as the current user in a new network namespace whose only
# interface is a loopback that is down, so no host can be reached
# (Unix-domain sockets, such as asyncio's self-pipe, still work). Creating
# the namespace needs root; setpriv then returns to this user's uid, gid
# and groups, so file modes and permission errors apply as for the user.
# sudo drops PATH, HOME and LD_* variables, so they are passed on
# explicitly.
set -euo pipefail
exec sudo -E unshare --net -- \
	setpriv --reuid="$(id -u)" --regid="$(id -g)" --init-groups -- \
	env PATH="$PATH" HOME="$HOME" TMPDIR="${TMPDIR:-/tmp}" LD_LIBRARY_PATH="${LD_LIBRARY_PATH:-}" "$@"
