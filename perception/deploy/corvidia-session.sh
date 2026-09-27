#!/usr/bin/env bash
# Selectable GDM Xorg session: one console and one directly owned Openbox.
# The existing onboard mission service is never started, stopped, or changed.
set -euo pipefail

project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
python="$project_dir/.venv/bin/python"
console="$project_dir/.venv/bin/corvidia-console"
wm_pid=
console_pid=

cleanup() {
    if [[ -n "$wm_pid" ]]; then
        kill -TERM "$wm_pid" 2>/dev/null || true
        wait "$wm_pid" 2>/dev/null || true
    fi
}
interrupted() {
    trap '' HUP INT TERM
    if [[ -n "$console_pid" ]]; then
        kill -TERM "$console_pid" 2>/dev/null || true
    fi
    exit "$1"
}
trap cleanup EXIT
trap 'interrupted 129' HUP
trap 'interrupted 130' INT
trap 'interrupted 143' TERM

fail() {
    local message="$*"
    printf '%s\n' "$message" >&2
    if [[ -n ${DISPLAY:-} ]] && command -v xterm >/dev/null 2>&1; then
        xterm -T 'Corvidia session unavailable' -geometry 72x18 \
            -e /bin/bash -c 'printf "%s\n\n%s\n%s\n" "$1" \
                "The onboard mission service was not changed." \
                "Press Enter to return to login; select Ubuntu to use the normal desktop."; IFS= read -r answer' \
            corvidia-session-error "$message" || true
    fi
    exit 1
}

[[ $# -eq 0 ]] || fail 'This graphical session does not accept command-line options.'
[[ $EUID -ne 0 ]] || fail 'Choose Corvidia from the normal user login screen; do not run this session as root.'
[[ -n ${DISPLAY:-} && ${XDG_SESSION_TYPE:-x11} == x11 ]] || fail 'This session requires an Xorg display provided by the login screen.'
[[ -x "$python" && -x "$console" ]] || fail 'The selected checkout has no installed Corvidia console environment.'
command -v openbox >/dev/null 2>&1 || fail 'Openbox is missing; select Ubuntu and finish installing the optional session.'
command -v xterm >/dev/null 2>&1 || fail 'xterm is missing; select Ubuntu and finish installing the optional session.'
command -v systemctl >/dev/null 2>&1 || fail 'The systemd user manager is unavailable.'

# Never use --replace or openbox-session: neither an existing window manager
# nor desktop autostart programs belong to this launcher.
openbox --config-file "$project_dir/deploy/corvidia-openbox.xml" &
wm_pid=$!
sleep 0.25
kill -0 "$wm_pid" 2>/dev/null || fail 'Openbox could not start. Select this session from the login screen, outside an existing desktop.'

systemctl --user is-active --quiet corvidia-stand.service || \
    fail 'The onboard stand service must already be running. Select Ubuntu and start the intended mode explicitly first.'
command -v loginctl >/dev/null 2>&1 || fail 'Cannot verify that the onboard service survives graphical logout.'
linger=$(loginctl show-user "$(id -u)" --property=Linger --value) || \
    fail 'Cannot read the user service lifetime setting.'
[[ "$linger" == yes ]] || fail 'User service lingering must already be enabled so closing the console does not end the onboard service.'

# One read-only status request, with the same local/private-session validation
# as the console. Do not print its token or import display variables into systemd.
mode=$("$python" - 2>&1 <<'PY'
from corvidia_perception.stand_cli import ApiClient, ClientError, DEFAULT_SESSION, load_session
try:
    status = ApiClient(load_session(DEFAULT_SESSION)).status()
except ClientError:
    raise SystemExit("Cannot read the existing local mission service or its private session file.")
if status.get("control_authority") != "onboard" or status.get("mission_api") != 2:
    raise SystemExit("The existing service must provide onboard mission API 2.")
mode = status.get("mode")
if mode == "telemetry":
    raise SystemExit("The service is in read-only telemetry mode. Select Ubuntu to choose a mission mode explicitly.")
if mode not in ("observe", "hardware"):
    raise SystemExit("The existing service has an unsupported mode.")
print(mode)
PY
) || fail "${mode:-The console could not attach to the existing mission service. Select Ubuntu to inspect it.}"

args=("$console")
case "$mode" in
    hardware) args+=(--hardware) ;;
    observe) ;;
    *) fail 'The service mode could not be verified; no console was launched.' ;;
esac

cd -- "$project_dir"
# Keep the GUI as this graphical session's child, never a detached user unit.
# Closing it ends this session and its Openbox; onboard execution continues.
"${args[@]}" &
console_pid=$!
result=0
wait "$console_pid" || result=$?
console_pid=
[[ $result -eq 0 ]] || fail "The console exited with status $result. The graphical session will not restart it."
exit 0
