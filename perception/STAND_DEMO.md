# Person-search stand demo

This demo joins the Jetson camera, YOLO, Cosmos, saved evidence, and the STM32's
bounded all-four motor interface. The operator describes a person's visible
appearance and submits one mission. The screen gives turning and
hold instructions; the operator turns the guarded stand using its external
handle. A confirmed, saved image ends the mission and requests zero motor input.
The Jetson owns the complete mission after Start. The Mac is an optional debug
and command terminal; the mission continues if that connection closes.

This is a stand demonstration. All four motors receive the same fixed demo
input. Guidance changes what the operator does with the stand; it does not send
flight-controller movement commands.

## Current headless setup

The validated hardware backend remains running on the Jetson with its last
mission completed. The operator confirmed that all four motors stopped and
unplugged the ESC battery. No new mission starts automatically. Use the
[maintenance terminal commands](#maintenance-terminal-commands) to inspect
status, watch guidance, explicitly submit another mission, or stop one. The
backend stays running when the terminal or SSH connection closes; the STM32
firmware remains v5. A future powered run requires fresh readiness confirmation.

## Launch the native console on the Jetson

Open a terminal on the Jetson desktop:

```sh
cd ~/corvidia/perception
./deploy/stand-console.sh
```

This opens the native `corvidia-console` window with a live camera view, person
appearance prompt, mission status, manual turn/HOLD guidance, saved-image result,
and explicit Stop. There are no motor-input or duration controls. Enter a visible
description such as **person wearing a red shirt**, then submit the mission once.

The launcher starts the backend in observation mode if it is inactive. It keeps
an existing service running and refuses a mode mismatch. With an already running
service, `.venv/bin/corvidia-console` launches the same window directly. The
Jetson desktop Python environment needs Tkinter and Pillow for the window.

For a separately commissioned hardware-mode service, use
`./deploy/stand-console.sh --hardware`. It requires that service to be active
already and presents the five physical readiness checks for each mission. The
launcher never switches an observation service into hardware mode.

## Modes and fixed demo profile

| Mode | Camera and Cosmos | STM32 | Start button |
| --- | --- | --- | --- |
| `observe` | Real perception | Simulated motors; optional read-only IMU | Runs the mission without motor output |
| `telemetry` | Status/preflight | Read-only MAVLink | Disabled |
| `hardware` | Real perception | Explicit bounded motor interface | Available after readiness checks |

Hardware mode is selected when launching the service; the console cannot turn
an observation service into a hardware service.

Start with `uv run corvidia-stand --mode observe`. Add `--port` with the STM32's
stable path for real read-only IMU observations while motor commands remain
simulated. Without that option, the dashboard labels IMU and motor status as
simulated. Use `--mode telemetry --port /dev/serial/by-id/DEVICE` for the dedicated
read-only check. Hardware commissioning uses a separately launched `--mode hardware`
service with that serial path.

The onboard demo profile is fixed internally at **5% equal motor input** with a
**60-second deadline**. A matching saved capture normally stops the mission
before that limit. User interfaces and mission packets accept a person
description and hardware readiness observations; they accept no power or timing
overrides. The model cannot change this profile, extend a run, or authorize a
new mission. Percentage is requested input, not measured RPM or electrical power.
The installed v5 firmware remains unchanged.

## Connection and deployment

Keep the existing Jetson deployment, environment, models, and captures until
the replacement checkout passes validation. The old `~/corvidia` deployment
was copied without Git metadata; it cannot simply run `git pull`. Create a new
Git checkout beside it, reconcile any different source files, and retain the
old directory for rollback. Do not use `rsync --delete` over the working setup.

The Python environment must see the Jetson's system OpenCV/GStreamer and
TensorRT. It also needs `pymavlink` and `pyserial` for telemetry or hardware
mode. Model engines and evidence remain outside the checkout. Reuse the healthy
Cosmos service on `127.0.0.1:8010` after checking its health.

Select the STM32 through its stable `/dev/serial/by-id/` path. Exactly one process
owns that serial device. Do not run the old motor-test CLI or a separate IMU
viewer concurrently with the stand service.

### Managed service

From the chosen checkout's `perception` directory on the Jetson:

```sh
./deploy/stand.sh start observe
./deploy/stand.sh status
./deploy/stand.sh logs
./deploy/stand.sh stop
```

The wrapper creates a temporary `corvidia-stand.service` under the current user's
systemd manager. It has **no automatic restart and no boot enablement**. An
existing active service is refused rather than replaced. Stop sends the normal
service shutdown signal and allows 15 seconds for cleanup; it does not use a
broad process-name kill. Starting the service leaves motors idle until an explicit
mission submission passes the onboard preflight checks.

The Jetson user manager must have lingering enabled so an SSH logout does not
terminate the service. This has been configured for `jlam`; verify with
`loginctl show-user jlam -p Linger`. On another installation, run
`loginctl enable-linger USERNAME` once. The wrapper checks this before starting.
Lingering preserves the user manager across logouts; the temporary stand unit
still has no boot enablement or automatic mission restart.

When exactly one matching Feather F405 (`0483:5740`) is present, the wrapper
selects its stable USB path automatically. In observation mode this adds real,
read-only IMU telemetry; motors remain simulated. If the controller cannot be
identified uniquely, observation uses simulated telemetry, while telemetry and
hardware modes require an explicit matching stable path.

```sh
./deploy/stand.sh start telemetry /dev/serial/by-id/DEVICE
# Only after guarded-stand commissioning, explicitly select hardware mode:
./deploy/stand.sh start hardware /dev/serial/by-id/DEVICE
```

The wrapper checks the existing Cosmos health endpoint before starting; it does
not launch, restart, or terminate Cosmos. The service journal contains the
private dashboard URL, including its operator token. View it with `logs`, but
keep that token out of shared logs, screenshots, and repository files.
The wrapper reads the Jetson's volatile journal by user-unit name and current
UID, so it also works when separate per-user journal files are unavailable.

The dashboard listens only on the Jetson loopback interface. For an optional
Mac debug view, forward its port through SSH, using the same local and remote port:

```sh
ssh -N -L 8080:127.0.0.1:8080 JETSON_SSH_ALIAS
```

Open the operator URL printed by the service, using `http://127.0.0.1:8080/` on
the Mac. Keep its `?token=...` value when opening it the first time. The page
stores that token for the tab and removes it from the address bar. Do not share
the operator link. Reopening without the token gives a view without controls.

## Operator sequence

1. Run the observation mode first. Confirm the live view, the interpreted person
   description, guidance, saved frame, and completed mission without motor output.
2. Check telemetry with ESC power disconnected. Confirm the v5 firmware identity,
   fresh IMU, idle state, zero requested inputs, and the expected capabilities.
3. Complete the separate guarded-stand commissioning checks before using
   hardware mode. The validated hardware test used removed propellers and
   secured motors. Software cannot verify the guard, restraint, external handle,
   or power disconnect. Nobody
   turns or approaches the drone directly while motors are energized.
4. In the native console, enter the person's visible appearance and review the
   target and preflight status. The service supplies the fixed demo profile;
   there are no power or timing fields.
5. Confirm all five readiness observations. Start only with startup complete,
   all motors still, and the guarded area clear. Use the external handle to
   follow the displayed guidance; hold still while confirmation completes.
6. A saved confirmation, deadline, operator stop, or fault ends the run. Use
   **Stop** to request zero input. Fresh zero-input telemetry is separate from
   physical stopping: observe the motors before approaching.

The onboard supervisor maintains the motor watchdog while its camera, model,
storage, and serial checks remain healthy. Closing the console, dashboard, terminal, SSH,
or Ethernet connection does not stop a started mission. Use explicit **Stop**
to end it early; a matching capture, fault, or the fixed deadline also ends it.
An ended mission does not automatically restart when a client or hardware
reconnects. If zero-input stop verification fails, new missions are
blocked until the operator recovers the physical setup and restarts the service.

## Maintenance terminal commands

The text client is an alternative for maintenance on the Jetson. From the
deployment's `perception` directory:

```sh
.venv/bin/python -m corvidia_perception.stand_cli status
.venv/bin/python -m corvidia_perception.stand_cli run "person wearing a red shirt"
.venv/bin/python -m corvidia_perception.stand_cli watch
.venv/bin/python -m corvidia_perception.stand_cli stop
```

`run` completes the applicable readiness checks, submits one appearance-only
mission request, and returns its accepted mission ID. The receipt does not
confirm motor motion. The onboard service performs preflight and then
handles detection, appearance confirmation, capture, and motor supervision.
The command returning or its SSH connection closing does not stop that mission.
The same internal demo profile applies. A matching committed capture, fault,
explicit stop, or the fixed deadline ends the run. An ambiguous response is
reported without retrying or issuing an automatic Stop. No separate preparation
or start command is sent, and no STM32 firmware change is needed.

`watch` displays status and manual turn/HOLD guidance. Ctrl+C exits only the
watcher. Use the explicit `stop` command or dashboard **Stop** to request zero
input. Stopping a watcher or unplugging Ethernet is not an emergency-stop
command; keep the physical power disconnect accessible.

For a commissioned hardware-mode service, add `--hardware` to `run` and complete
its interactive readiness confirmations. The flag does not change the service
mode. Observation mode retains simulated motor output. The private operator
session at `~/.local/state/corvidia/stand-8080.json` stays on the Jetson; the CLI
reads it without putting its token in command arguments or printed output.

### Headless operation

The backend and text client can run with the Jetson desktop logged out. The
native camera console needs a desktop session. Save desktop work before logging
out; the managed backend stays running through the user manager's lingering
setting. Use the onboard maintenance commands, optionally through the Mac's
SSH wrapper, to submit, watch, or stop a headless mission. The mission and its
watchdogs still run entirely on the Jetson.

In the validated setup, logging out the desktop increased available memory from
about 1.4 GiB to 1.9 GiB. The existing **1536 MiB** memory guard was unchanged.
The guard still applies to every mission; headless operation does not bypass
preflight or hardware readiness checks.

The backend's systemd unit removes `DISPLAY`, `WAYLAND_DISPLAY`, and
`XAUTHORITY` from its environment. A lingering user manager can retain these
variables after desktop logout; stale display settings caused Argus to fail
with `Failed to initialize EGLDisplay` on restart. Removing them restored real
camera startup headlessly. The separate native console keeps its desktop display
environment.

### Optional Mac debug terminal

The local wrapper simply launches the same onboard commands over the verified
`corvidia-jetson` SSH alias. It needs local `python3` to quote arguments safely:

```sh
./perception/deploy/stand-terminal.sh status
./perception/deploy/stand-terminal.sh run "person wearing a red shirt"
./perception/deploy/stand-terminal.sh watch
./perception/deploy/stand-terminal.sh stop
```

The wrapper defaults to `status`. It creates no forwarding tunnel, copies no
operator token, and runs no local mission or watchdog loop. Arguments reach
`~/corvidia/perception` literally, including spaces and quotes in descriptions.
Hardware readiness requires a real interactive terminal; only that command
requests an SSH TTY. `watch` is read-only, and its disconnection leaves the
onboard mission running. The explicit `stop` command works from another terminal.

## Completion and evidence

A mission succeeds only when the target's confirmation and image record are
committed for the current mission. An old event, raw detector box, timed-out
answer, or failed write must not report success. The saved capture is the
camera image, accompanied by metadata; it is not a screenshot of the browser.

The event pipeline stores a full frame and the crop submitted to Cosmos.
Its optional best-shot image may be written later when a track ends, so the
motor stop must not wait for it. Evidence is retrieved by the current mission
identifier rather than by accepting an arbitrary filesystem path.

There is no motor RPM feedback. UI messages say **zero inputs confirmed**,
not that physical stopping was measured. IMU telemetry is in the raw sensor
frame and does not establish flight readiness.

## HTTP contract

`StandWebServer` forwards commands to a nonblocking supervisor callback. It has
no serial or model connection of its own. All mutation requests require
`Content-Type: application/json`, the `X-Corvidia-Token` header, an accepted
loopback Host, and a matching Origin when one is supplied. Cross-site browser
requests are rejected. JSON request bodies are limited to 4096 bytes.

| Endpoint | Body or result |
| --- | --- |
| `GET /api/status` | Current supervisor snapshot |
| `GET /frame.jpg` | Latest preview JPEG, or 503 while waiting |
| `POST /api/mission` | `appearance`, optional hardware `readiness`; returns `accepted` and new `mission_id` |
| `POST /api/stop` | `mission_id` |
| `GET /api/evidence?mission_id=…` | Authorized committed image; requires token header |

Mission readiness contains exactly `guarded_stand`, `hands_clear`,
`power_disconnect_accessible`, `motors_still`, and `esc_startup_finished`, each
the JSON boolean `true` for hardware mode. Observation mode omits physical
assertions; the supervisor enforces readiness according to its immutable mode.
The console clears these observations after submission. Mission requests accept
no extra fields, including power or duration overrides. The old prepare/start
endpoints are retired. Commands enqueue `{"action": "mission|stop", ...}` and
return 202; this acknowledges queue admission, not motor motion or a completed
mission. Check the returned mission ID in subsequent status.

## Validation before a powered demonstration

The existing remote baseline passed 68 non-GPU tests before integration. That
establishes the existing event behavior, not stand-control readiness. Validate:

- Only a current, confirmed and committed event completes the current mission.
- Storage failure, camera loss, backend failure, operator Stop, deadline,
  supervisor loss, and serial loss end the run without automatic restart.
- Duplicate Start and repeated packets cannot extend or revive the fixed run.
- Missing token, wrong Host/Origin, oversized input, malformed readiness, and
  any power or timing override never reaches the supervisor command queue.
- Run the real camera/Cosmos workflow in observation mode, then inspect fresh
  read-only USB telemetry with motor power disconnected.
- Separately commission the guarded hardware setup. Record the requested input,
  duration, capture-to-stop latency, ACK, fresh zero telemetry, and the operator's
  physical stop observation. A test remains incomplete without that observation.

## Integration validation — 2026-09-27 (earlier architecture)

These records describe the earlier externally leased implementation. The
operator-lease behavior is superseded by onboard mission ownership; these checks
do not establish the new disconnect behavior. Camera, model, and read-only
firmware observations remain historical evidence.

The integration includes upstream `perception-pipeline` commit `f0cf748` (tracks
publisher and bridge). The Jetson release lives at
`~/corvidia-releases/stand-20260927-0626`, with its own environment using system
OpenCV and TensorRT. Git history was transferred from the authenticated Mac;
direct GitHub authentication on the Jetson is still unconfigured.

- **203 tests passed; 7 GPU/camera tests excluded** on the Jetson. The tests
  cover the existing pipeline, new upstream bridge, mission state machine,
  motor protocol simulation, web API, and failure paths.
- The real IMX477 camera and YOLO engine produced fresh frames, typically
  about 26–50 ms old during the checks.
- The real STM32 v5 stream was healthy at about 50 Hz. All four requested motor
  inputs and `DS_ACTIVE` stayed zero; `DS_TOKEN` stayed 1. Read-only serial
  verification also checked that the adapter sent no bytes.
- Observation missions ended correctly on a two-second deadline, operator Stop,
  and loss of the operator lease. Motor execution and stop verification were
  simulated during these checks; IMU telemetry was real.
- A separate real Cosmos request rejected the live workbench scene in 1.43 s
  while YOLO ran. The backend returned idle and the camera stayed fresh. This
  was a negative backend check, not a detected-person mission.
- Completion tests verify that an existing motor fault or operator cancellation
  cannot be hidden by a matching capture. Evidence records distinguish frame
  capture-to-stop time from evidence commit-to-stop time.

At that time, still pending were a person in the live camera view for the
matching-description, committed-image, and simulated early-stop rehearsal; visual browser inspection;
and separate guarded hardware commissioning. No motor commands were sent during
this integration validation. Browser inspection was blocked by denied browser
access to the local dashboard.

Runtime audit files and images are under `~/corvidia-data/integration`,
`~/corvidia-data/stand-missions`, and `~/corvidia-data/stand-events` on the Jetson.
They are not committed to Git.

## Status of the upstream autodrone bridge

Commit `f0cf748` is included in this checkout and on the Jetson. Its
[`corvidia_bridge.py`](bridge/corvidia_bridge.py) publishes perception data to
the separate `autodrone` framework: tracks, free space, health, captures, and
optional preview frames. It has no serial connection or motor command handling.
The native console and maintenance terminal submit to the stand supervisor
through its local operator API. That supervisor remains the single owner of the STM32 connection.

The external `autodrone` project is not installed on this Jetson, and its flight
controller command contract has not been inspected. Before enabling that path:

- Obtain the framework repository and check its FC adapter against the installed
  v5 firmware. The firmware accepts bounded bench commands; flight setpoints and
  mission commands remain disabled. MAVLink transport alone does not establish
  command compatibility.
- Share one camera/model process. The stand worker currently sends internal
  events, while the upstream bridge expects UDP ports 5601/5602, `/health`, and
  an optional `/stream`. The stand API has `/api/status` and `/frame.jpg`, and
  stand depth inference is disabled. Do not launch both camera owners together.
- Preserve mission ID and requested appearance when forwarding captures. The
  bridge's current capture message omits those fields and uses placeholder
  pose and probability values. It can wait five seconds for a best-shot image;
  the stand's immediate stop uses the committed current-mission event directly.
- Use the real Cosmos confirmer for a matching-person demonstration. The bridge
  README's example defaults to the stub confirmer and produces unverified
  captures.

Keep the onboard supervisor responsible for motor authority, fixed deadlines,
health watchdogs, and stop verification when integrating the external mission framework.

### Link to the tested STM32 firmware

The stand already links mission decisions to the installed v5 interface through
[`MotorSession`](src/corvidia_perception/stand_motor.py). An explicit operator
start establishes one bounded run; the adapter obtains the firmware token,
sets that run's envelope, and sends the existing all-four MAVLink bench command.
A committed matching capture, stop request, fault, or deadline ends that run.
The MCU's own watchdogs remain responsible for enforcing output limits.

The console, maintenance clients, and a future external mission adapter must use this
single supervisor. The model may supply target judgments and manual turn cues;
it does not select raw motor output, extend an active run, or authorize a new
start. This stand path requires no STM32 firmware update. Autonomous flight
control remains outside the tested bench interface.

### Terminal validation — 2026-09-27 (superseded client-owned runs)

The private-tunnel, lease, and Ctrl+C-stop workflow below was tested before the
move to onboard missions. Current `run` and `watch` semantics are documented
above; these results do not validate them.

- **262 tests passed; 7 GPU/camera tests excluded** on the Jetson, including
  terminal-to-HTTP-to-supervisor tests with simulated motors.
- The Mac wrapper reached the live Jetson service through its private SSH
  tunnel. Separate status commands worked while that tunnel was occupied.
- Live observation runs verified a two-second client limit, a ten-second
  mission deadline, and Ctrl+C during an active search. Stop verification
  succeeded with simulated motors, and each wrapper removed its own tunnel.
- The real STM32 stayed in read-only mode: `DS_TOKEN=1`, `DS_ACTIVE=0`, and all
  four reported motor inputs were zero. Its v5 firmware was not modified.
- No matching-person capture was completed in these terminal runs. That live
  rehearsal and separate powered stand commissioning were still pending then.

## Onboard mission validation — 2026-09-27

The current single-submit API completed a real observation mission after the
operator authorized desktop logout. The console process and desktop session
were closed; the managed backend survived through lingering. Available memory
rose from about 1.4 GiB to 1.9 GiB without reducing the 1536 MiB guard. The onboard
text client submitted the mission through SSH, with the Mac acting only as a
debug terminal.

- Mission `5d41d2ef26fb42d9aeacf7f7eba040ba` detected and confirmed a real person,
  committed `frame.jpg`, `crop.jpg`, and `event.json`, and completed with a
  simulated motor stop.
- Evidence commit to simulated stop took **23.5 ms**; source frame capture to
  simulated stop took **1977 ms**. These are observation-mode timings, not
  measurements of physical motor stopping.
- Read-only telemetry from the real STM32 still reported zero requested input
  on all four motors, with `DS_TOKEN=1`. The firmware was unchanged.
- Native-console display checks cover a 1920×1080 desktop and a compact
  560×390 window. Mocked display checks establish layout and controls, not
  powered operation.

Evidence is on the Jetson under:

```text
~/corvidia-data/stand-events/stand-64ff44db077741d2b279096f9cf317c1/6231a015-41a0-431d-82a7-9981d4b20083/
```

The operator confirmed propellers removed and motors secured for the next
hardware test. A fresh headless hardware-service restart passed preflight with
ESC power disconnected: real firmware v5, `IMU_OK=1`, `DS_FAULT=0`,
`DS_ACTIVE=0`, all four requested inputs zero, `DS_TOKEN=1`, a fresh camera,
and about 1741 MiB available memory. This also verified the service environment
fix for Argus after desktop logout.

### Powered full-pipeline result

After physical readiness confirmation, hardware mission
`8fcb310874374742ab775261f8964611` completed the full person-search workflow:
fixed **5% input**, real camera/YOLO/Cosmos confirmation, committed evidence,
and automatic motor stop. The fixed 60-second maximum remained in force; the
matching capture ended the run earlier. The operator confirmed that all four
motors ran smoothly and stopped automatically, then unplugged the ESC battery.

| Measurement | Result |
| --- | --- |
| Motor start to stop command | 20.8375 s |
| Stop acknowledgement latency | 9.57 ms |
| Motor start to fresh zero-input telemetry | 21.4679 s |
| Evidence commit to stop request | 15.29 ms |
| Source frame capture to stop request | 1630.61 ms |
| Lowest sampled available memory | 1693.05 MiB |
| Highest sampled frame age | 95.8 ms |

Fresh telemetry confirmed zero requested inputs on all four motors,
`DS_ACTIVE=0`, and `DS_FAULT=0`. Every sampled IMU report had `IMU_OK=1`.
The timing measurements describe software commands, acknowledgements, and
telemetry; the operator separately confirmed physical stopping. This validates
the props-off stand workflow at the fixed demo input, without establishing
autonomous-flight readiness or propeller-loaded behavior.

The committed `frame.jpg`, `crop.jpg`, and `event.json` are on the Jetson in:

```text
~/corvidia-data/stand-events/stand-7715660e874f43aea53d9006289e7de3/bcfcd659-9fb2-469f-93b6-9d9d07a4066e/
```

The run audit is
`~/corvidia-data/integration/headless-full-pipeline-hardware.json`.
The hardware backend remains running in its completed state with no automatic
mission restart; ESC power is disconnected. The STM32 firmware was unchanged.
