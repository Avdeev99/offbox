#!/bin/sh
set -eu

export XDG_CONFIG_HOME=/home/dev/.config
export XDG_STATE_HOME=/home/dev/.local/state
export XDG_CACHE_HOME=/home/dev/.cache
export MUTAGEN_DATA_DIRECTORY=/home/dev/.mutagen-smoke
export UV_PROJECT_ENVIRONMENT=/tmp/offbox-smoke-venv
install -d -m 0700 /home/dev/.ssh
install -m 0600 /keys/client /home/dev/.ssh/id_ed25519
install -m 0644 /keys/client.pub /home/dev/.ssh/id_ed25519.pub
uv sync --frozen --no-editable --project /checkout
mutagen daemon run > /artifacts/mutagen-daemon.log 2>&1 &
daemon_pid=$!
deadline=$(($(date +%s) + 30))
while [ ! -S "$MUTAGEN_DATA_DIRECTORY/daemon/daemon.sock" ]; do
    if ! kill -0 "$daemon_pid" 2>/dev/null || [ "$(date +%s)" -ge "$deadline" ]; then
        printf '%s\n' 'Isolated Mutagen daemon did not become ready.' >&2
        exit 1
    fi
    sleep 0.1
done
exec "$UV_PROJECT_ENVIRONMENT/bin/python" /checkout/tests/smoke/controller.py
