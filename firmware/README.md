# Corvidia STM32F405 firmware

**Current installed firmware: v5.** It supports selectable operator envelopes through 100% input and
60,000 ms, defaulting to 5% / 500 ms. See [DSHOT_V5.md](DSHOT_V5.md) and the
[installation status](../docs/references/control/dshot-v5-install-2026-09-27.md).
V5 all-four tests at 5% and 7.5% for one second, 50% for 200 ms and two
seconds, and 20% for ten seconds passed user-observed spin/stop checks. This is bench firmware; mission/LLM flight outputs remain unavailable.

**V6 startup ramp is deferred.** Its opt-in source profile is retained in
[DSHOT_V6.md](DSHOT_V6.md), but it has not been installed or verified with
powered motors. The active Jetson adapter requires v5; the prepared v6 host
changes are archived outside this checkout. V5 applies requested input
immediately. The default PlatformIO build remains the IMU-only `feather_f405`
profile, so v5 and v6 motor-capable images both require explicit selection.

PlatformIO + STM32duino firmware for the Adafruit Feather STM32F405 and BNO085.
See [IMPORT.md](IMPORT.md) for provenance and [MOTOR_CONTROL.md](MOTOR_CONTROL.md)
for motor wiring, bench interlocks, and the future movement interface.

**DShot individual and all-four bench pulses passed observed spin/stop checks**
at 1% input for 200 ms, followed by all four at 5% for 500 ms. See the [test record](../docs/references/control/dshot-powered-tests-2026-09-27.md).
Legacy PWM remains blocked after its earlier motor-4 incident. Disconnect the ESC
battery between bench sessions.

## Firmware builds

| Environment | Behavior |
| --- | --- |
| `feather_f405` (default) | IMU + MAVLink over USB; no motor outputs |
| `feather_f405_bench` | Motor-test protocol with hardware outputs disabled |
| `feather_f405_bench_pwm` | Explicit PWM bench image; no IMU or flight stabilization |
| `feather_f405_pwm_diagnostic` | Measures output-pad pulse timing; ESC battery must be disconnected |
| `feather_f405_dshot_diagnostic` | DShot diagnostic with signal pads held low |
| `feather_f405_dshot_signal` | DShot300 zero-throttle frames only; ESC battery must be disconnected |
| `feather_f405_dshot_imu` | IMU + bounded command protocol; ESC signals disabled |
| `feather_f405_dshot_imu_signal` | IMU + zero DShot frames; nonzero commands disabled |
| `feather_f405_dshot_imu_bench` | IMU + explicitly enabled individual or all-four tests; short bench pulses verified |
| `feather_f405_dshot_imu_ramp` | Deferred v6 candidate with a fixed five-second startup ramp; not installed or powered-verified |
| `native` | Hardware-independent C++ tests |
| `native_ramp` | Hardware-independent ramp tests compiled with the v6 identity/capability |

See [DSHOT.md](DSHOT.md) for the new STM32 driver, its MAVLink diagnostics and
validation limits. The combined individual/all-four DShot bench image is now v5, flashed and
verified byte-for-byte; see the installation record for live verification status. It includes the IMU sequence freshness
fix. Live attitude/quaternion telemetry runs at about 50 Hz; both test modes
are reported available, all startup inputs are zero, and stop is acknowledged. Switching test modes needs no reflash.
Both modes require explicit commands and default to a 5% input / 500 ms envelope.
V5 supports explicit envelopes up to 100% / 60 seconds; input is not measured RPM. DShot execution requires per-test readiness; simultaneous
execution also requires individual physical stop checks. Flight control is unavailable.

The [mission control candidate](MISSION_CONTROL.md) adds selectable mission
duration, a configurable ceiling up to 100%, a separate nominal collective,
bounded LLM movement intents and session expiry. It is tested offline and is not
connected to ESC outputs or dispatched by the installed firmware.

The default source sends HEARTBEAT, ATTITUDE, ATTITUDE_TARGET and STATUSTEXT.
ATTITUDE_TARGET is a command echo, not a measured attitude. The default source
now gates ATTITUDE on separate attitude/gyro freshness checks. The shared IMU
wrapper uses game rotation vector (relative yaw), validates reports and clears
health on reset. Message arrival alone does not prove sensor capture latency.

The board observed on September 27 also sent ATTITUDE_QUATERNION, SYS_STATUS,
SERVO_OUTPUT_RAW, SYSTEM_TIME and EXTENDED_SYS_STATE. That running image has not
yet been matched to source. It was backed up and restored byte for byte after
the PWM bench investigation; its 50 Hz attitude stream was verified again.
It has since been backed up again before installing the zero-throttle DShot
diagnostic. Do not assume the original image implements the local bench protocol.

```sh
cd firmware
pio run -e feather_f405
pio test -e native
```

## Live MAVLink IMU dashboard

From the repository root:

```sh
python3 -m venv .venv-control
.venv-control/bin/python -m pip install -r firmware/tools/requirements-control.txt
.venv-control/bin/python firmware/tools/imu_viz.py
```

Open **http://127.0.0.1:8765**. The dashboard provides:

- 3D orientation, roll/pitch/yaw in degrees, gyro rates in degrees/second.
- A 15-second gyro plot, message rate, packet age, heartbeat and board messages.
- Reported gyro/accelerometer health and reported output pulses when available.
- A stale indication after 500 ms without valid ATTITUDE; automatic USB reconnect.
- DShot mode, IMU freshness, command limits enabled/disabled, faults and requested
  motor inputs when the integrated firmware is installed. Motor RPM is unavailable.

It only listens. It sends no MAVLink packets, heartbeats, arming or motor commands.
The HTTP server binds to localhost. No external web assets are needed.
Only one application should own the serial port: close any serial monitor first,
and stop this dashboard with Ctrl+C before running the motor tool.

Auto-detection only selects STM32 USB VID/PID `0483:5740`. It refuses to guess
between multiple boards. Override with `--port /dev/cu.usbmodem...`; source IDs
default to `1/1` and can be set using `--system` and `--component`.

### Interpreting the display

MAVLink ATTITUDE uses radians and rad/s; the dashboard converts both to degrees.
Axes follow the reported sensor frame. Verify physical sensor mounting and the
sensor-to-airframe transform before using this orientation for stabilization.
A roll near ±180° means the reported frame is inverted relative to zero roll;
the visualizer deliberately does not hide that with an automatic zero offset.

SYS_STATUS health flags and SERVO_OUTPUT_RAW values are firmware reports.
They do not independently measure I2C freshness, ESC acceptance, or motor RPM.
An unknown value is shown as unavailable. Disconnection dims the last orientation.

Protocol reference: [MAVLink common messages](https://mavlink.io/en/messages/common.html#ATTITUDE).

## Uploading

See the full [bench instructions](MOTOR_CONTROL.md) before changing images.
The Feather F405 enters ROM DFU with **BOOT0 tied to 3.3 V, then RESET**.
Remove the BOOT0 jumper before resetting into the application. Disconnect ESC
telemetry from RX during DFU; the ROM bootloader also listens on serial interfaces.
Keep motor power disconnected during flashing.

Manufacturer: [Feather F405 DFU details](https://learn.adafruit.com/adafruit-stm32f405-feather-express/dfu-bootloader-details).

## Host checks

```sh
.venv-control/bin/python -m unittest discover -s firmware/tools -p 'test_*.py' -v
```

These cover MAVLink wire decoding, unit conversion, stale/disconnected display
state, source filtering, rejected invalid samples, reconnect state, and motor
command bounds. They do not replace hardware verification.
