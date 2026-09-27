# Person-search stand demo

This demo joins the Jetson camera, YOLO, Cosmos, saved evidence, and the STM32's
bounded all-four motor interface. The operator describes a person's visible
appearance, prepares the mission, then starts it. The screen gives turning and
hold instructions; the operator turns the guarded stand using its external
handle. A confirmed, saved image ends the mission and requests zero motor input.

This is a stand demonstration. All four motors receive the same operator-selected
input. Guidance changes what the operator does with the stand; it does not send
flight-controller movement commands.

## Modes and limits

| Mode | Camera and Cosmos | STM32 | Start button |
| --- | --- | --- | --- |
| `observe` | Real perception | Simulated motors; optional read-only IMU | Runs the mission without motor output |
| `telemetry` | Status/preflight | Read-only MAVLink | Disabled |
| `hardware` | Real perception | Explicit bounded motor interface | Available after preparation and readiness checks |

Hardware mode is selected when launching the service; the web page cannot turn
an observation service into a hardware service.

Start with `uv run corvidia-stand --mode observe`. Add `--port` with the STM32's
stable path for real read-only IMU observations while motor commands remain
simulated. Without that option, the dashboard labels IMU and motor status as
simulated. Use `--mode telemetry --port /dev/serial/by-id/DEVICE` for the dedicated
read-only check. Hardware commissioning uses a separately launched `--mode hardware`
service with that serial path.

- Mission motor input: greater than zero, at most **20%**; default **5%**.
- Fixed maximum duration: **1–60 seconds**; default **10 seconds**.
- The LLM does not select motor input, extend the deadline, or authorize Start.
- Installed v5 firmware supports a broader protocol range up to 100%; this
  stand application deliberately caps its own requests at 20%.
- Input percentage is neither measured RPM nor electrical power. Previous
  successful bench runs do not establish thermal or mechanical limits for
  every duration, especially with propellers installed.

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
broad process-name kill. Starting the service leaves motors idle until a prepared
mission receives an explicit Start.

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

The dashboard listens only on the Jetson loopback interface. Forward its port
through SSH, using the same local and remote port:

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
   hardware mode. The reported setup has propellers installed; software cannot
   verify the guard, restraint, external handle, or power disconnect. Nobody
   turns or approaches the drone directly while motors are energized.
4. In the hardware dashboard, enter the person's visible appearance, motor input,
   and maximum duration. A visible appearance description is required.
   Select **Prepare mission** and review the target and preflight status.
5. Confirm all five readiness observations. Start only with startup complete,
   all motors still, and the guarded area clear. Use the external handle to
   follow the displayed guidance; hold still while confirmation completes.
6. A saved confirmation, timeout, operator stop, or fault ends the run. **Stop**
   and the **Escape** key request zero input. Fresh zero-input telemetry is
   separate from physical stopping: observe the motors before approaching.

Keep the dashboard visible. It renews the operator lease every 250 ms; the
supervisor's one-second lease expires if the connection or page stalls. Hiding
or closing the page sends a best-effort stop, and a hidden page stops renewing
the lease. An ended mission does not automatically restart when the page or
hardware reconnects. If zero-input stop verification fails, new missions are
blocked until the operator recovers the physical setup and restarts the service.

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
| `POST /api/prepare` | `appearance`, `percent`, `duration_ms` |
| `POST /api/start` | `mission_id`, `readiness` |
| `POST /api/stop` | `mission_id` |
| `POST /api/lease` | `mission_id` |
| `GET /api/evidence?mission_id=…` | Authorized committed image; requires token header |

Start readiness contains exactly `guarded_stand`, `hands_clear`,
`power_disconnect_accessible`, `motors_still`, and `esc_startup_finished`, each
the JSON boolean `true` for hardware mode. Observation mode omits physical
assertions; the supervisor enforces readiness according to its immutable mode.
The UI clears these
observations after preparation and starting. Commands enqueue
`{"action": "prepare|start|stop|lease", ...}` and return 202; this acknowledges
queue admission, not motor motion or a completed mission. Check status afterward.

## Validation before a powered demonstration

The existing remote baseline passed 68 non-GPU tests before integration. That
establishes the existing event behavior, not stand-control readiness. Validate:

- Only a current, confirmed and committed event completes the current mission.
- Storage failure, camera loss, backend failure, operator Stop, deadline,
  supervisor loss, and serial loss end the run without automatic restart.
- Duplicate Start and repeated packets cannot extend or revive the fixed run.
- Missing token, wrong Host/Origin, oversized input, malformed readiness, and
  input above 20% never reach the supervisor command queue.
- Run the real camera/Cosmos workflow in observation mode, then inspect fresh
  read-only USB telemetry with motor power disconnected.
- Separately commission the guarded hardware setup. Record the requested input,
  duration, capture-to-stop latency, ACK, fresh zero telemetry, and the operator's
  physical stop observation. A test remains incomplete without that observation.

## Integration validation — 2026-09-27

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

Still pending: a person in the live camera view for the matching description,
committed-image, and simulated early-stop rehearsal; visual browser inspection;
and separate guarded hardware commissioning. No motor commands were sent during
this integration validation. Browser inspection was blocked by denied browser
access to the local dashboard.

Runtime audit files and images are under `~/corvidia-data/integration`,
`~/corvidia-data/stand-missions`, and `~/corvidia-data/stand-events` on the Jetson.
They are not committed to Git.
