#!/usr/bin/env bash
# Launch the native console from a terminal on the Jetson desktop.
set -euo pipefail

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
mode=observe
if [[ $# -eq 1 && "$1" == --hardware ]]; then
    mode=hardware
elif [[ $# -ne 0 ]]; then
    echo 'Usage: deploy/stand-console.sh [--hardware]' >&2
    exit 2
fi
[[ -x "$project_dir/.venv/bin/corvidia-console" ]] || {
    echo 'The native console entry point is missing; install this checkout in its Jetson environment first.' >&2
    exit 1
}
systemctl --user show-environment >/dev/null 2>&1 || {
    echo 'Run this launcher in the Jetson desktop session with its systemd user manager available.' >&2
    exit 1
}
state=$(systemctl --user is-active corvidia-stand.service 2>/dev/null) || true
case "$state" in
    active|activating|reloading) ;;
    inactive|failed|unknown)
        if [[ "$mode" == hardware ]]; then
            echo 'Hardware service must already be explicitly commissioned and started with deploy/stand.sh start hardware.' >&2
            exit 1
        fi
        "$project_dir/deploy/stand.sh" start observe
        ;;
    *) echo "Stand service state is $state; it was not changed. Wait for shutdown or inspect it explicitly." >&2; exit 1 ;;
esac

"$project_dir/.venv/bin/python" - "$mode" <<'PY'
import sys
import time
from corvidia_perception.stand_cli import ApiClient, ClientError, DEFAULT_SESSION, load_session

deadline = time.monotonic() + 10
while True:
    try:
        status = ApiClient(load_session(DEFAULT_SESSION, required=False)).status()
    except ClientError:
        if time.monotonic() >= deadline:
            raise SystemExit("Stand service did not expose status within 10 seconds; inspect deploy/stand.sh logs.")
        time.sleep(.2)
        continue
    if status.get("mode") != sys.argv[1]:
        raise SystemExit(f"Active service mode is {status.get('mode', 'unknown')}; expected {sys.argv[1]}. "
                         "The service was not stopped or replaced.")
    if status.get("control_authority") != "onboard" or status.get("mission_api") != 2:
        raise SystemExit("The service must provide onboard mission API 2; update it explicitly first.")
    break
PY
if [[ "$mode" == hardware ]]; then
    exec "$project_dir/.venv/bin/corvidia-console" --hardware
fi
exec "$project_dir/.venv/bin/corvidia-console"
