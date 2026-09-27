#!/usr/bin/env bash
# Local operator: its lease crosses Ethernet, so a lost link expires the run.
set -euo pipefail
umask 077

perception_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if [[ $# -eq 0 ]]; then set -- status; fi

# These one-shot operations own no lease and can run in a second terminal
# while the local run client owns port 8080. Never interpolate operator text
# into a remote command: only these fixed command strings are accepted.
case "$1" in
    status)
        if [[ $# -eq 1 ]]; then
            remote_command='cd ~/corvidia/perception && exec .venv/bin/python -m corvidia_perception.stand_cli status'
        elif [[ $# -eq 2 && "$2" == --json ]]; then
            remote_command='cd ~/corvidia/perception && exec .venv/bin/python -m corvidia_perception.stand_cli status --json'
        else
            echo 'Usage: stand-terminal.sh status [--json]' >&2
            exit 2
        fi
        exec ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=5 \
            corvidia-jetson "$remote_command"
        ;;
    stop)
        if [[ $# -ne 1 ]]; then
            echo 'Usage: stand-terminal.sh stop' >&2
            exit 2
        fi
        exec ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=5 \
            corvidia-jetson 'cd ~/corvidia/perception && exec .venv/bin/python -m corvidia_perception.stand_cli stop'
        ;;
    run) ;;
    *) echo 'Usage: stand-terminal.sh {status [--json]|run APPEARANCE [OPTIONS]|stop}' >&2; exit 2 ;;
esac

python3 - <<'PY'
import socket
import sys

if sys.version_info < (3, 12):
    raise SystemExit("stand-terminal requires local Python 3.12 or newer")
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", 8080))
        probe.listen(1)
    except OSError:
        raise SystemExit("Local port 8080 is occupied. Close its known tunnel/service first; "
                         "stand-terminal will not reuse an existing listener.")
PY

# Keep the socket path short enough for macOS's Unix-domain path limit.
terminal_tmp=$(mktemp -d /tmp/corvidia-stand.XXXXXXXX)
chmod 700 "$terminal_tmp"
control_socket="$terminal_tmp/ssh"
session_file="$terminal_tmp/session.json"
cli_pid=
cleanup() {
    result=$?
    trap - EXIT
    trap '' INT TERM HUP
    if [[ -n "$cli_pid" ]]; then
        kill -TERM "$cli_pid" 2>/dev/null || true
        # Keep forwarding alive for the CLI's bounded stop/zero verification.
        # A stalled client cannot keep this shell waiting indefinitely.
        for ((i = 0; i < 50; i++)); do
            if ! kill -0 "$cli_pid" 2>/dev/null; then break; fi
            sleep 0.1
        done
        if kill -0 "$cli_pid" 2>/dev/null; then
            kill -KILL "$cli_pid" 2>/dev/null || true
        else
            wait "$cli_pid" 2>/dev/null || true
        fi
    fi
    if [[ -S "$control_socket" ]]; then
        ssh -S "$control_socket" -O exit corvidia-jetson >/dev/null 2>&1 || true
    fi
    rm -rf -- "$terminal_tmp"
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

if ! ssh -M -S "$control_socket" -o ControlMaster=yes -o ControlPersist=no \
    -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=5 \
    -o ExitOnForwardFailure=yes -o ServerAliveInterval=1 -o ServerAliveCountMax=2 \
    -fNT -L 127.0.0.1:8080:127.0.0.1:8080 corvidia-jetson; then
    echo 'Could not open a private Jetson tunnel. Check corvidia-jetson SSH access and local port 8080.' >&2
    exit 1
fi

# ProxyCommand=false prevents opening a different connection if our master died.
if ! ssh -S "$control_socket" -o ControlMaster=no -o ProxyCommand=false -o BatchMode=yes \
    corvidia-jetson 'cat "$HOME/.local/state/corvidia/stand-8080.json"' > "$session_file"; then
    echo 'Could not read the running stand service session through this tunnel.' >&2
    exit 1
fi
chmod 600 "$session_file"

# Preserve interactive input while allowing TERM/HUP to interrupt the wait.
# Cleanup lets the CLI verify stop before closing this wrapper's tunnel.
PYTHONPATH="$perception_dir/src" python3 -m corvidia_perception.stand_cli \
    --session "$session_file" "$@" <&0 &
cli_pid=$!
result=0
wait "$cli_pid" || result=$?
cli_pid=
exit "$result"
