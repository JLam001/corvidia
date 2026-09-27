# DShot + IMU operator test firmware v5

V5 is an integrated bench-testing image. It is **not autonomous-flight firmware**.
It retains raw sensor-frame IMU telemetry and individual/all-four tests, and adds
an explicit operator envelope for each test. Installation status is recorded in
[the v5 installation record](../docs/references/control/dshot-v5-install-2026-09-27.md).

## Available range and defaults

| Setting | Default envelope | Supported envelope |
| --- | --- | --- |
| Throttle input | Up to 5% | Greater than 0, up to 100% |
| Fixed test duration | 20–500 ms | 20–60,000 ms |
| Selection | Motor 1 by default | Individual 1–4 or explicit all-four |

The CLI's default test remains 1% for 200 ms. The user requested up to one minute
for stand demos. The maximum duration is a fixed deadline, not a continuously
renewed action. Throttle percent is a normalized command, not measured RPM or
electrical power. V4 tests through 5% / 500 ms and initial v5 all-four tests at 5% and 7.5%
for 1000 ms, plus later 50% pulses at 200 ms and 2000 ms and a 20% / 10-second run, have passed operator-observed
spin/stop checks. The supported range does not establish safe thermal, battery or motor
limits for a longer run. No higher-power test is automatic after upload.

Boot, stop and expiry restore the default envelope. Each test needs a new
single-use enable token. A run cannot be restarted or extended by a repeated
command. The three-second enable window applies before a run starts; an active
run has its own selected deadline (up to 60 seconds).

The existing independent timer checks remain: main-loop freshness 50 ms,
attitude/gyro receive age 100 ms each, GCS heartbeat age 500 ms. A stop zeros all
four requested inputs. Faults cannot be cleared by merely sending late keepalives.
DMA faults halt output, but physical signal-loss behavior and failure injection
are still unverified. There is no RPM or thermal feedback.

## MAVLink contract

- New heartbeat custom mode **0x44534935** (`DSI5`) and `FW_REV=5` distinguish
  this build from v4 (`DSIM`). Source/target rules stay operator 255/190 → 1/1.
- USER_1 / 31010 enable: `[1, token, limit_percent, limit_ms, 0, 0, 0]`.
  Both limit fields zero select the legacy default 5% / 500 ms. Otherwise both
  must be supplied and valid. Firmware and the timer lease both enforce them.
- USER_2 / 31011 individual: `[motor, percent, duration_ms, token, 0, 0, 0]`.
- USER_3 / 31012 all-four: `[4, percent, duration_ms, token, 0, 0, 0]`.
- USER_1 with seven zeros stops; no flight arming is accepted.
- Mission USER_4/USER_5, flight setpoints and ESC configuration commands remain
  unsupported. Offline mission-control software is not routed to motor outputs.

Named telemetry adds `DS_MAXP=100`, `DS_MAXMS=60000`, `DS_CAP_BP` (current
envelope percent × 100), `DS_CAP_MS`, `FLT_READY=0`, and `AXES_OK=0`.
The source publishes raw IMU axes; measured dominant mounting signs are recorded
in the [axis check](../docs/references/control/imu-alignment/README.md), but precise
attitude/rate alignment is not yet applied. The dashboard displays these limits.

## Host usage

Dry run (does not open serial):

```sh
.venv-control/bin/python firmware/tools/dshot_motor_test.py \
  --all-motors --percent 1 --duration-ms 500
```

For a different envelope, add `--limit-percent P --limit-duration-ms MS` and keep
the requested input/duration within those limits. The maximum accepted envelope
is 100 and 60000. This is selectable without reflashing v5.

Executing a test additionally requires `--port PORT --execute --bench-ready`.
All-four execution requires `--individual-stops-checked` after observing each
individual motor's correct motion and complete stop. Keep props removed, motors
secured, and hands/cables clear for bench tests. Those flags record operator
readiness; the firmware cannot verify the physical setup.

The host identifies the image and checks fresh health, idle state, zero inputs,
capabilities and a single-use token. It rejects an extended envelope on v4. After
the pulse it requests stop, checks the ACK and fresh zero/fault-free reports, and
sends a final zero request when closing. No command is automatically retried.
Physical stopping still requires operator observation.

Stop-only:

```sh
.venv-control/bin/python firmware/tools/dshot_motor_test.py --port PORT --stop --execute
```

Validation: 70 native C++ tests, 43 Python tests, JavaScript syntax check, and
successful builds of the bench, zero-only and output-disabled integrated profiles.
Tests include a simulated 60-second run at full protocol range, default/explicit
envelopes, late refresh, fixed deadlines, replay, v4 rejection and zero-status checks.
These are software tests, not physical full-power or one-minute motor tests.
