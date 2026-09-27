#!/usr/bin/env bash
# Stand mission service. Starting the service never starts a motor mission.
set -euo pipefail

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
python="$project_dir/.venv/bin/python"
unit=corvidia-stand.service

usage() {
  cat <<'EOF'
Usage: deploy/stand.sh start [observe|telemetry|hardware] [/dev/serial/by-id/DEVICE]
       deploy/stand.sh status
       deploy/stand.sh logs
       deploy/stand.sh stop

Start defaults to observe (simulated motors). A unique Feather F405 is selected
for read-only IMU telemetry when present. Hardware requires the explicit
"hardware" argument and each mission's five readiness observations in the UI.
The unit is temporary, has no automatic restart, and is not enabled at boot.
EOF
}

fail() { printf '%s\n' "$*" >&2; exit 1; }

feather_device() {
  local properties
  [[ -c "$1" ]] || return 1
  properties=$(udevadm info --query=property --name="$1" 2>/dev/null) || return 1
  [[ $'\n'"$properties"$'\n' == *$'\nID_VENDOR_ID=0483\n'* ]] || return 1
  [[ $'\n'"$properties"$'\n' == *$'\nID_MODEL_ID=5740\n'* ]] || return 1
  [[ $(basename -- "$1") == *FEATHER_F405* ]]
}

action=${1:-status}
if [[ $action == --help || $action == -h ]]; then usage; exit 0; fi
[[ $action == start || $action == status || $action == logs || $action == stop ]] || { usage >&2; exit 2; }
command -v systemctl >/dev/null || fail 'This wrapper requires the Jetson systemd user manager.'
systemctl --user show-environment >/dev/null 2>&1 || fail 'The systemd user manager is unavailable in this login session.'

case "$action" in
  start)
    [[ $# -le 3 ]] || { usage >&2; exit 2; }
    mode=${2:-observe}
    [[ $mode == observe || $mode == telemetry || $mode == hardware ]] || fail 'Mode must be observe, telemetry, or hardware.'
    state=$(systemctl --user show "$unit" --property=ActiveState --value)
    case "$state" in
      active|activating|deactivating|reloading) fail "The stand service is $state. It will not be replaced; stop it explicitly first." ;;
    esac
    [[ -x "$python" ]] || fail "Python environment missing: $python"
    command -v systemd-run >/dev/null || fail 'systemd-run is unavailable.'
    command -v udevadm >/dev/null || fail 'udevadm is required to identify the USB controller.'

    serial=${3:-}
    if [[ -n $serial ]]; then
      [[ $serial == /dev/serial/by-id/* ]] || fail 'Use the stable /dev/serial/by-id/ path for the STM32.'
      feather_device "$serial" || fail 'The selected path is not the expected Feather F405 USB device (0483:5740).'
    else
      candidates=()
      shopt -s nullglob
      for device in /dev/serial/by-id/*FEATHER_F405*; do
        if feather_device "$device"; then candidates+=("$device"); fi
      done
      if [[ ${#candidates[@]} -eq 1 ]]; then
        serial=${candidates[0]}
      elif [[ $mode != observe ]]; then
        fail 'No unique Feather F405 was found. Supply its stable serial path explicitly.'
      else
        printf '%s\n' 'No unique Feather selected; observation mode will simulate both IMU and motor status.'
      fi
    fi

    "$python" - <<'PY' || fail 'Cosmos is not healthy at 127.0.0.1:8010. Check the existing service; this wrapper does not restart it.'
import json
import urllib.request
try:
    with urllib.request.urlopen("http://127.0.0.1:8010/health", timeout=3) as response:
        healthy = json.load(response).get("status") == "ok"
except Exception:
    healthy = False
raise SystemExit(0 if healthy else 1)
PY

    args=("$python" -m corvidia_perception.stand --mode "$mode" --http-port 8080)
    if [[ -n $serial ]]; then args+=(--port "$serial"); fi
    systemd-run --user --unit="$unit" --description='Corvidia stand mission' \
      --collect --service-type=exec --property="WorkingDirectory=$project_dir" \
      --property=Restart=no --property=TimeoutStopSec=15s \
      --setenv=PYTHONUNBUFFERED=1 -- "${args[@]}"
    printf 'Stand service launched in %s mode. Motors require an explicit mission start.\n' "$mode"
    printf '%s\n' 'Run deploy/stand.sh logs for the private operator link; do not publish that token.'
    ;;
  status)
    [[ $# -le 1 ]] || { usage >&2; exit 2; }
    systemctl --user status "$unit" --no-pager --lines=0 || true
    if [[ -x $python ]]; then
      "$python" - <<'PY'
import json
import urllib.request
try:
    with urllib.request.urlopen("http://127.0.0.1:8080/api/status", timeout=3) as response:
        print(json.dumps(json.load(response), indent=2))
except Exception as exc:
    print("Dashboard status unavailable:", type(exc).__name__)
PY
    fi
    ;;
  logs)
    [[ $# -le 1 ]] || { usage >&2; exit 2; }
    printf '%s\n' 'Logs may contain the private operator URL. Keep its token out of shared logs and repository files.'
    # Jetson's volatile journal may not expose per-user journal files. Match
    # the user-unit fields in the system journal, scoped to this user's UID.
    journalctl "_SYSTEMD_USER_UNIT=$unit" "_UID=$(id -u)" --no-pager --lines=60
    ;;
  stop)
    [[ $# -le 1 ]] || { usage >&2; exit 2; }
    systemctl --user stop "$unit"
    printf '%s\n' 'Service stop completed. Observe physical motor stopping before approaching the stand.'
    ;;
esac
