# DShot300 + IMU bench firmware

This page records the v4 design and commissioning. The current v5 source adds
operator-selectable envelopes; see [DSHOT_V5.md](DSHOT_V5.md) for its limits,
protocol and installation status. V4's verified physical test range remains
5% input for 500 ms.

The driver runs in the STM32 firmware. MAVLink carries commands and telemetry
between the host and STM32; the ESC keeps its AM32 firmware.

```text
Jetson / host -- MAVLink --> STM32 -- DShot300 --> ESC --> motors
```

## Current stage

The integrated source includes BNO085 attitude/gyro telemetry and bounded,
operator-only individual and simultaneous all-four motor commands. It does not run a flight controller, mixer,
position hold, or LLM movement requests. The `feather_f405_dshot_imu_signal`
zero-throttle image has now been flashed with ESC battery disconnected; all
59,132 payload bytes match the DFU readback. The board boots and reports
`DS_OUTPUT=1`, `DS_ALLOW=0`, `DS_FAULT=0`, and zero requested input on every channel.
The first 15-second capture contained no ATTITUDE packets and reported
`IMU_OK=0` with both age fields `-1`: IMU initialization failed. A full USB power
cycle produced the same result. A second zero-only build is now installed with a
startup delay, probes for both documented sensor addresses, initialization-stage
telemetry, restoration of 400 kHz after the library resets the bus to 100 kHz,
and DShot timer startup after IMU initialization. Its 59,404-byte payload matches
the DFU readback. It boots and identifies the sensor at `0x4A` with `IMU_INIT=5`,
but the accepted reports become stale. The installed image is now the
`feather_f405_dshot_imu_bench` v4 build: it includes the sequence-based freshness
fix and both individual and all-four tests. All 59,740 payload bytes match the
DFU readback. A 15-second live check received 749 ATTITUDE and 749 quaternion
packets (about 50 Hz each), fresh attitude/gyro ages of 2/1 ms, `IMU_REJ=0`,
`DS_FAULT=0`, `DS_ALLOW=1`, `DS_ALL=1` and zero requested input on every channel.
Each motor passed an individual 1% input (DShot 67), 200 ms test. After those
checks, the same pulse was sent to all four together. Test and stop commands were
acknowledged each time; the operator confirmed the correct motors spun smoothly
and stopped after all five pulses. See the [powered test record](../docs/references/control/dshot-powered-tests-2026-09-27.md).
A subsequent all-four test at 5% input for 500 ms also passed operator-observed
smooth motion and complete stop, with healthy IMU telemetry and zero final inputs.
These observations do not verify RPM, signal waveform timing, sustained operation,
fault recovery or flight control. The original firmware backup remains available.

The old PWM host tool remains blocked after the
[motor-4 stop failure](../docs/references/control/motor-session-2026-09-27.md).
DShot commissioning requires explicit readiness for each invocation, fresh
firmware/IMU readiness and zero reported input on all four channels. Simultaneous
execution additionally requires confirmation of the four individual physical
stop checks. These checks are operator observations, not RPM telemetry.

The user asked to treat the setup as if propellers were installed. Props were
confirmed removed, motors secured, and startup stationary before the first test.
A throttle cap is not an RPM limit: motor speed and physical stop are not measured.

## Build profiles

| Environment | IMU | ESC output | Nonzero commands |
| --- | --- | --- | --- |
| `feather_f405` (repository default) | Yes | None | Rejected |
| `feather_f405_dshot_imu` | Yes | Pads held low | Rejected |
| `feather_f405_dshot_imu_signal` | Yes | Zero DShot frames | Rejected |
| `feather_f405_dshot_imu_bench` | Yes | Bounded DShot | Explicit enable required |
| `feather_f405_dshot_diagnostic` | No | Pads held low | Rejected |
| `feather_f405_dshot_signal` | No | Zero DShot frames | Rejected |

The bench profile explicitly sets `CORVIDIA_DSHOT_SIGNALS=1`,
`CORVIDIA_DSHOT_BOUNDED=1`, and `CORVIDIA_DSHOT_MOTOR_TESTS=1`. It exists for
validation; a successful build does not approve powered use. No profile arms
for flight. The old PWM bench profile and host protocol remain separate.

## Command bounds and stop conditions

- Individual tests select one ESC channel; the other three receive zero.
- An explicit all-four command applies the same bounded input to all four
  channels with one shared duration and stop deadline. Both modes are present
  in the same bench image; changing modes does not require another flash.
  Run the individual motion and physical-stop checks before using all-four mode.
  The firmware cannot independently certify those physical checks.
- Input greater than 0 and at most **5%**; duration **20–500 ms**.
- Host dry-run defaults: **1% input, 200 ms**. No ramp, calibration, reversing,
  or reserved DShot setting commands are exposed.
- Percent input maps to `48 + floor(1999 * percent / 100)`: 1% is DShot 67,
  5% is 147. This does not predict RPM; even low inputs may spin quickly.
- Three-second boot gate after IMU initialization, then a fresh operator
  heartbeat and healthy attitude AND gyro are required to enable a test.
- Each enable uses a new token and lasts at most three seconds. One test consumes
  the enable. Retried commands cannot restart or extend that test.
- The timer checks the fixed run deadline, a **50 ms** main-loop refresh limit,
  **100 ms** age limits for each IMU report and a **500 ms** operator-heartbeat
  limit. A late refresh cannot revive an expired test.
- Stop/disarm, unhealthy IMU, a reported IMU reset, or an expired deadline cancels
  the test. Starting again requires a fresh enable/token.

TIM5 runs the frame service at 1 kHz independently of the main loop. On expiry it
continues sending zero frames. Deadlines take effect on the next timer service
and frame, subject to interrupt latency. Blocking I2C with interrupts still
running does not extend the run. Globally masked interrupts, CPU/clock failure,
DMA failure and physical ESC behavior remain hardware validation concerns.
DMA errors latch an output fault and force pads low; that fallback depends on
the ESC's unverified signal-loss behavior. A stop ACK is not proof of stopped motors.

## IMU integration

BNO085 runs over I2C at 400 kHz. Game rotation vector and calibrated gyro reports
are requested at 200 Hz. Game rotation vector avoids magnetometer correction;
its yaw is relative and can drift. This changes the shared IMU wrapper used by
the default source image too. The newly installed zero-throttle diagnostic uses
this wrapper. V4 passes the IMU freshness check after the v1 initialization
failure and v2 timestamp-filter failure.

The wrapper tracks attitude and gyro arrival times separately, rejects invalid
values and duplicate/out-of-order report sequence numbers, normalizes valid quaternions,
and clears health on a sensor reset. Each update handles at most four reports.
The underlying library may block within one transaction; the motor timer is
separate. Freshness uses receipt time and report sequence progression, not an
independently synchronized measurement of sensor capture latency.

The v3 source fixes a live-test finding: Adafruit's I2C HAL does not populate
its `t_us` output, so SH-2 reconstructed timestamps cannot serve as the monotonic
sample clock assumed in v1/v2. The wrapper now uses the documented 8-bit report
sequence with wrap handling and retains separate MCU receive-time deadlines.
Reference: [Adafruit I2C implementation](https://github.com/adafruit/Adafruit_BNO08x/blob/master/src/Adafruit_BNO08x.cpp),
[SH-2 report sequence definition](https://github.com/adafruit/Adafruit_BNO08x/blob/master/src/sh2_SensorValue.h).

The integrated image sends ATTITUDE and ATTITUDE_QUATERNION at a target 50 Hz
while both reports are fresh. USB backpressure can reduce telemetry rate; old
telemetry is not queued indefinitely. The quaternion is the sensor frame;
mounting rotation and body-axis alignment are still unverified. A failed initial
IMU setup requires investigation/restart and never enables a motor test.

## MAVLink interface

Board IDs: system/component `1/1`. Operator IDs: `255/190`, with GCS heartbeats.
Only exactly addressed COMMAND_LONG packets are processed. Source filtering is
not authentication; this bench interface assumes a trusted local USB connection.
Heartbeat custom mode is `0x4453494D` (`DSIM`); the zero-only diagnostic remains
`0x44533330` (`DS30`). Flight arming stays disabled even during a bench test.

| Command | Parameters 1–7 |
| --- | --- |
| USER_1 / 31010: enable | `1, token, 0, 0, 0, 0, 0` |
| USER_1 / 31010: stop | `0, 0, 0, 0, 0, 0, 0` |
| USER_2 / 31011: individual test | `motor (1–4), percent, duration_ms, token, 0, 0, 0` |
| USER_3 / 31012: all-four test | `4, percent, duration_ms, token, 0, 0, 0` |
| COMPONENT_ARM_DISARM: stop | `0, 0, 0, 0, 0, 0, 0` |

Read the next token from `DS_TOKEN` before enabling, and use that same token for
the test. Accepted enables increment the next token. Tokens are single-use within
one boot and are not a security credential; never replay queued requests across a
reboot. Enable/test packets require `confirmation=0`; clients must not retry on
ACK loss. Timeout recovery sends stop and requires a new explicit test.

DO_MOTOR_TEST (the old PWM command), arming, and autonomous setpoints are not
accepted by the integrated image. Custom USER commands keep percent input,
DShot values, PWM microseconds and RPM from being confused.

All-four operation uses a distinct command; invalid individual motor IDs never
select all motors. The host requires a fresh `DS_ALL=1` capability report for an
all-four test, so older firmware cannot be mistaken for an image supporting it.
This capability flag does not certify physical motor behavior.

NAMED_VALUE_INT reports include `IMU_OK`, `ATT_AGE`, `GYR_AGE`, `DS_OUTPUT`,
`DS_ALLOW`, `DS_ALL`, `DS_FAULT`, `DS_TOKEN`, `DS_ACTIVE`, `DS_EXPIRE`, `DS_FRAMES`, and
`M1_DS` through `M4_DS`. The second diagnostic image also reports `IMU_INIT`
(0 not attempted, 1 no address, 2 both addresses, 3 sensor handshake failed,
4 report setup failed, 5 configured), `IMU_ADDR` and `I2C_MASK` (bit 0: 0x4A,
bit 1: 0x4B). V4 also reports raw report counters `IMU_ATT`, `IMU_GYR` and
rejection count `IMU_REJ`. Status rotates at 25 packets/s (each field roughly every 0.88 s
with these diagnostics); it can miss a short motor pulse. Inputs are requested values, not measured
RPM. Frame counts record completed DMA transfers, not ESC acknowledgements.
The read-only IMU dashboard recognizes the new image and displays these fields.

## Pins and resources

| ESC input | Feather pin | Timer channel |
| --- | --- | --- |
| 1 | 9 / PB8 | TIM4 CH3 |
| 2 | 10 / PB9 | TIM4 CH4 |
| 3 | 6 / PC6 | TIM3 CH1 |
| 4 | 5 / PC7 | TIM3 CH2 |

The user confirmed this rewiring and installation. Actual channel-to-motor
response still needs verification. No additional rewiring is prescribed.

Timer DMA bursts use DMA1 stream 2/channel 5 for TIM3 and stream 6/channel 2 for
TIM4. Transfers are non-circular and finish with low tails. The integrated image
also owns TIM5. It does not link the PWM driver, NeoPixel output, or flight mixer.
Current Wire calls do not use DMA; review these resource assignments before
adding DMA-backed I2C or other peripherals.

## Build and checks

Local validation on September 27, 2026: all three integrated profiles, the
default IMU image and both original DShot diagnostic profiles built successfully.
The latest bench/signal/interlocked builds pass. All 57 native C++ tests and
28 Python tests passed; the dashboard JavaScript
passed syntax checking. Firmware builds still emit warnings from vendored
MAVLink packed-member helpers. Dashboard rendering and hardware behavior have
not been verified for this integration.

From the repository root:

```sh
pio run -d firmware -e feather_f405_dshot_imu -e feather_f405_dshot_imu_signal -e feather_f405_dshot_imu_bench
pio test -d firmware -e native
.venv-control/bin/python -m unittest discover -s firmware/tools -p 'test_*.py' -v
.venv-control/bin/python firmware/tools/dshot_motor_test.py --motor 1 --percent 1 --duration-ms 200
.venv-control/bin/python firmware/tools/dshot_motor_test.py --all-motors --percent 1 --duration-ms 200
```

The final two commands only print plans and do not open serial. `--motor` and
`--all-motors` are mutually exclusive. Individual mode remains the default.
To execute a bounded DShot commissioning request, add `--port PORT --execute
--bench-ready` after confirming props are removed, motors secured, startup
stationary and hands clear. All-four mode additionally requires
`--individual-stops-checked` after observing each motor's correct motion and stop.
The old PWM tool's incident interlock remains disabled and is not shared with
DShot commissioning. Stop-only execution is available for the correctly
identified integrated image. No motor command is automatically retried.

Hardware checks remain: with ESC battery disconnected, measure DShot bit timing,
packet contents, zero tails and channel routing while the IMU runs. Exercise
command/heartbeat/IMU/main-loop timeouts and reset behavior on the output signals.
Record physical motion and stop after each powered pulse; an ACK only confirms
firmware command acceptance.

References: [DShot specification](https://betaflight.com/docs/wiki/guides/current/Dshot),
[MAVLink common commands](https://mavlink.io/en/messages/common.html),
[STM32F405 timer/DMA mapping (ST manual excerpt)](https://betaflight.com/assets/files/stm32f405_pins_timers_dma-e7ae8fce49c50af781149b0458e4ac87.pdf).
