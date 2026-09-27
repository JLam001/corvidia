#!/usr/bin/env bash
# Optional Mac/Linux SSH launcher. The Jetson owns the mission after Start.
set -euo pipefail

if [[ $# -eq 0 ]]; then set -- status; fi
case "$1" in
    status|run|watch|stop) ;;
    *) echo 'Usage: stand-terminal.sh {status|run APPEARANCE [OPTIONS]|watch|stop}' >&2; exit 2 ;;
esac

tty_args=(-T)
if [[ "$1" == run ]]; then
    for arg in "$@"; do
        if [[ "$arg" == --hardware ]]; then
            if [[ ! -t 0 || ! -t 1 ]]; then
                echo 'Hardware readiness confirmation requires an interactive terminal.' >&2
                exit 2
            fi
            tty_args=(-t)
            break
        fi
    done
fi

# SSH executes a remote shell string. Quote each argument instead of joining
# operator text directly into that string; descriptions remain literal data.
remote_args=$(python3 -c 'import shlex, sys; print(" ".join(shlex.quote(arg) for arg in sys.argv[1:]))' "$@")
remote_command='cd "$HOME/corvidia/perception" && exec .venv/bin/python -m corvidia_perception.stand_cli '"$remote_args"
exec ssh "${tty_args[@]}" -o BatchMode=yes -o StrictHostKeyChecking=yes \
    -o ConnectTimeout=5 -o ServerAliveInterval=1 -o ServerAliveCountMax=2 \
    corvidia-jetson "$remote_command"
