> **Legacy PWM testing remains stopped.** During the September 27 session the user
> reported sustained motor-4 rotation outside an active channel-1 test, despite
> acknowledged stop commands. Keep the ESC battery disconnected. PWM timing,
> channel mapping and the ESC stop setting must be verified before another
> PWM test. The PWM host CLI blocks spin requests before opening serial;
> dry runs and stop remain available. See [the session record](../docs/references/control/motor-session-2026-09-27.md).

# STM32 motor controls

## Current status

The installed image is [v5](DSHOT_V5.md), with IMU telemetry and bounded DShot
commands. The onboard mission demo uses 5% equal input with a 7.5% host cap;
see the [current console guide](../perception/STAND_DEMO.md). The optional
[v6 ramp](DSHOT_V6.md) is prepared but has not been installed. The sections
below retain the earlier v4 and PWM implementation history.

## Earlier v4 implementation

The [DShot300 integration](DSHOT.md) now combines IMU telemetry with bounded
MAVLink bench commands: individual or explicit all-four operation, at most 5%
input and 500 ms, plus independent
timer deadlines. Signal and motor-command interlocks default off. The combined
individual/all-four bench image (v4) was flashed and readback-verified. It
includes the IMU freshness fix; live IMU telemetry is about 50 Hz, both test
modes are available, startup input reports are zero, and stop is acknowledged.
Each individual motor and then all four together passed user-observed smooth
spin/stop checks at 1% input for 200 ms, followed by all four at 5% for 500 ms. See the [DShot test record](../docs/references/control/dshot-powered-tests-2026-09-27.md).
DShot requests require explicit bench readiness; all-four requests also require
individual physical stop checks. RPM and flight behavior are unverified. The PWM
bench and movement code below remain separate from it.

There are three separate pieces:

1. **Individual motor tests:** a dedicated STM32 firmware image and an operator
   CLI. One ESC output at a time, bounded pulse width and duration, explicit
   enable per test, heartbeat timeout, and an independent timer deadline.
2. **Movement proposals:** a strict JSON validator and matching C++ supervisor
   for level, bounded tilt, and yaw-rate requests. These are software previews.
3. **Controller and mixer:** host-tested angle/rate control math and an X-quad
   mixer. They require verified gains, fresh body-frame IMU values, a verified
   motor layout, and rotation directions. They are not connected to motor outputs.

The original `feather_f405` image continues to run the IMU/MAVLink application.
The legacy PWM bench image replaces that application while installed; it does not run
the IMU loop. No image here performs flight arming or autonomous flight.

## Corrected wiring

Disconnect the motor battery and USB before moving wires. Move only the
Feather ends of the two signal wires; leave all three phase wires per motor alone.

| ESC pad | Feather label | MCU pin / peripheral | Action |
| --- | --- | --- | --- |
| G | GND | Common signal ground | Keep |
| 1 | 9 | PB8 / TIM4 CH3 | Keep |
| 2 | 10 | PB9 / TIM4 CH4 | Keep |
| 3 | **6** | PC6 / TIM3 CH1 | Move from **11 to 6** |
| 4 | **5** | PC7 / TIM3 CH2 | Move from **12 to 5** |
| V | Disconnected | Battery voltage | Keep disconnected |
| C | A0, if used | PA4 / ADC | Optional; see below |
| T | RX, if used | PB11 / USART3 RX | Optional; see below |

Adafruit documents **no hardware PWM on D11/PC3 or D12/PC2**. D9/D10/D6/D5
have the timer channels listed above. SDA/PB7 and SCL/PB6 remain dedicated to the
IMU's I2C bus; sharing the TIM4 peripheral's other alternate-function pin options
does not require switching those I2C pins to PWM. Do not use `analogWrite()`
or servo pulse generation on D11/D12 as a substitute for this map.

The initial wiring had V disconnected and C/T connected. T was subsequently
disconnected for the DShot tests and current demo. The optional destinations
above are intended connections, not a verified observation of the actual wires.
C must be a current-sense output within the ADC's 0–3.3 V range. T must be a
compatible logic-level telemetry output. Their signal limits and the exact ESC
revision are not established by the supplied images; disconnect C/T for the
first test if these cannot be verified. This firmware does not read either pad.
With T attached, RX/PB11 is occupied by the ESC and cannot simultaneously serve
as the Jetson's UART RX. Keep the Jetson link on USB for now; TX is unused here.

The user has identified the ESC as a **Flywoo GOKU G45M 45A 3–6S AM32 4-in-1**.
See [the model investigation](../docs/references/control/ESC-G45M-AM32.md). The
installed settings and firmware version remain unknown. The PWM test profile requires ordinary
unidirectional RC PWM, with **1000 microseconds as stop**. A reversible/3D ESC
configuration may interpret that differently and must not use this profile.
Confirm the installed ESC configuration before enabling output. No automatic
full-throttle endpoint calibration is provided.

Battery positive/negative power only the ESC's battery pads. Never connect ESC
V/battery positive to Feather 3V, BAT, USB, or a GPIO. Use board silkscreen labels
and electrical continuity; the supplied generated diagrams have conflicting
arrows and duplicated/misaligned labels.

Sources: [Adafruit pinout](https://learn.adafruit.com/adafruit-stm32f405-feather-express/pinouts),
[STM32duino Feather variant](https://github.com/stm32duino/Arduino_Core_STM32/blob/main/variants/STM32F4xx/F405RGT_F415RGT/variant_FEATHER_F405.h),
[HardwareTimer API](https://github.com/stm32duino/Arduino_Core_STM32/wiki/HardwareTimer-library).

## Planned motor layout

View from above, camera looking toward the top of this diagram. This matches
the user's statement that 1/3 are rear and 1/2 are right when looking forward
from behind the camera.

```text
                   CAMERA / FRONT
           ESC 4                     ESC 2
           front-left                front-right

           ESC 3                     ESC 1
           rear-left                 rear-right
```

`include/airframe.h` stores these planned positions. The user has confirmed all
four motors are mounted or clamped for testing. Final frame positions and
rotation directions have not been physically verified. Consequently `verified=false`
and all yaw signs are unset. The mixer rejects that configuration. Host tests
use an explicitly synthetic rotation pattern, not a measured configuration.

## Build and tests

From `firmware/`, with PlatformIO installed:

```sh
pio test -e native
python3 -m unittest discover -s tools -p 'test_control_tools.py'
pio run -e feather_f405                  # existing IMU/MAVLink image
pio run -e feather_f405_bench            # bench protocol, all output disabled
pio run -e feather_f405_bench_pwm        # explicit PWM-capable bench image
```

The platform is pinned to `ststm32@20.0.0`. The PWM-capable image starts at
1000 us on every channel; starting a motor additionally requires an accepted
operator enable and test command. The default project environment remains
`feather_f405`; it has no ESC output code.

On September 27 the installed firmware was backed up with two matching full-flash
reads. The PWM bench image was flashed and its 36,464-byte payload verified by
DFU readback. A powered channel-1 test (1020 µs, 0.5 seconds) and stop were
acknowledged; see [the session record](../docs/references/control/motor-session-2026-09-27.md)
for physical observations and subsequent results. Compilation and host
tests do not verify pulse timing, electrical wiring, actual stop behavior,
rotation direction, or stabilization. Measure the outputs before the first
powered test. An oscilloscope/logic analyzer should show 50 Hz frames and only
1000 us stopped pulses; this is the ESC **input** frame rate, distinct from its
internal motor PWM frequency.

## Operator motor test

The user confirmed propellers are removed, all four motors are mounted or clamped,
and the corrected signal wiring has been completed. Electrical continuity and
ESC PWM configuration have not been independently measured. Check the PWM
configuration above before selecting the PWM build. Do not flash with the motor battery connected.

When ready, flash the selected bench image using the board's documented DFU
procedure and PlatformIO `pio run -e feather_f405_bench_pwm -t upload`.
For this board Adafruit documents holding BOOT0 high (3.3 V) while resetting to
enter the ROM DFU bootloader; remove that connection/reset for normal operation.
The original imported README's double-tap instructions are not relied on here.
See [Adafruit DFU instructions](https://learn.adafruit.com/adafruit-stm32f405-feather-express/dfu-bootloader-details).
Disconnect ESC T from Feather RX while entering/flashing the USB bootloader:
Adafruit notes that serial traffic on RX can interfere with ROM bootloader selection.

Install the host tool dependencies into a virtual environment:

```sh
python3 -m venv .venv-control
.venv-control/bin/pip install -r tools/requirements-control.txt

# Default: print the requested test, without opening any serial port.
.venv-control/bin/python tools/motor_test.py --motor 1 --pulse-us 1050 --seconds 1

# Explicit operator-only execution, after the checks above.
# Replace the example port with the actual device; on Jetson it may be /dev/ttyACM0.
.venv-control/bin/python tools/motor_test.py --port /dev/cu.usbmodemEXAMPLE \
  --motor 1 --pulse-us 1050 --seconds 1 --execute --bench-ready

# Stop requires no bench-ready acknowledgement.
.venv-control/bin/python tools/motor_test.py --port /dev/cu.usbmodemEXAMPLE --stop --execute
```

The tool identifies the bench firmware before sending commands, checks ACKs,
keeps sending operator heartbeats, and sends stop on normal exit or interruption.
It never retries a test automatically. A 1050 us pulse may not start a given
motor; do not respond by blindly increasing limits or performing high-throttle
calibration. Acknowledge and investigate ESC configuration and waveform first.

### Enforced limits

| Check | Behavior |
| --- | --- |
| Explicit enable | Eight-second window; cannot be extended by duplicate enables |
| Test | One channel, 1000–1100 us, 20–2000 ms through the protocol |
| Test completes | Stop all channels and revoke enable |
| Operator heartbeat | Stop/revoke after 500 ms; a late heartbeat cannot resume |
| Main loop stalls | TIM5 guard requests stopped pulses after 100 ms; latches until another operator enable |
| Output frame | Stopped compare takes effect by the next 20 ms PWM frame |
| Invalid command | Reject non-finite values, fractional motor IDs, out-of-range pulses, multi-motor sequences, wrong targets |
| Flight arm/setpoint | Unsupported/ignored in bench firmware |

The timer guard depends on functioning interrupts and the MCU clock; it is not
an independent hardware safety device. The bench image deliberately avoids IMU
I2C calls and NeoPixel transfers. This bench stop policy is not a flight failsafe.

### Wire protocol

Use MAVLink over USB. Firmware ID is system 1/component 1; the operator is
system 255/component 190. Its heartbeat uses `custom_mode=0x42454E43` (`BENC`).
Messages from the existing Jetson onboard-computer identity cannot activate
bench tests. These IDs separate roles, not authentication; serial access must
remain with the trusted operator tool, not an LLM shell.

- `MAV_CMD_USER_1` (31010): param1=1 enables a bench test; param1=0 stops/revokes.
  All other parameters zero. This is a project-local use of the USER command.
- `MAV_CMD_DO_MOTOR_TEST` (209): channel 1–4, PWM throttle type 1, pulse in us,
  duration in seconds, motor count 1, board order 2, param7=0. Only this subset
  is supported; no sequences. See [MAVLink common commands](https://mavlink.io/en/messages/common.html#MAV_CMD_DO_MOTOR_TEST).
- `MAV_CMD_COMPONENT_ARM_DISARM` with param1=0 stops; flight arming is rejected.

## Movement interface for the future LLM supervisor

The LLM proposes one action; deterministic code validates it. Human authority,
collective thrust, output enable, gain configuration, and mode changes remain
outside that interface. Decision inference does not run the rate loop.
`motion.schema.json` describes the three fields the model may propose; a trusted
supervisor adds `sequence` before passing the envelope to `motion_request.py`
or the C++ supervisor. No motor-test, throttle, or arm tool is offered to the model.

| Action | Amount | Effect |
| --- | --- | --- |
| `level` | 0 | Roll/pitch target zero, yaw rate zero |
| `tilt_forward` / `tilt_backward` | 0–10 degrees | Negative/positive pitch target |
| `tilt_left` / `tilt_right` | 0–10 degrees | Negative/positive roll target |
| `yaw_left` / `yaw_right` | 0–30 degrees/second | Negative/positive body yaw rate |

Every request has a duration of 1–1000 ms and a supervisor-assigned unsigned
sequence number. The supervisor must stamp sequence and receipt time outside
the model; do not accept queued stale decisions after a reconnect. Fresh local
supervisor keepalives are needed within 250 ms. They are independent of slow
LLM generation; they must not extend the action's absolute lifetime.

Example (preview only, no motors or serial):

```sh
python3 tools/motion_request.py \
  '{"action":"tilt_forward","amount":5,"duration_ms":500,"sequence":1}'
```

There is no `hover`, `move_meters`, `climb`, or `land` option: the current system
cannot regulate position or altitude. `level` does not brake drift. Preview
output explicitly reports `execution_available=false`. The C++ supervisor
provides limits, replay rejection within a session, expiry, and revocation.
The angle controller produces rate targets, then bounded rate PID corrections;
the mixer converts them to normalized motor demands. These software components
are currently exercised in tests only.

Before connecting that path to actuators, verify motor rotation, IMU-to-body
orientation, separate timestamped gyro/attitude freshness, control-loop timing,
gains and saturation handling, independent pilot override, and the response to
sensor/link loss. The existing IMU driver combines gyro/attitude freshness and
does not yet satisfy those flight-controller requirements.

## Verification recorded September 27, 2026

- All three STM32 build environments above compiled successfully with PlatformIO.
- All 35 native C++ tests passed, including 17 original tests and 18 new tests
  for motor limits, deadlines, protocol validation, motion expiry, controller
  checks, and mixer signs.
- All 10 host-tool tests passed, including wire command ID consistency, wrong-firmware rejection, disabled
  outputs, lost ACK handling without retry, and explicit stop.
- Motor and movement CLI dry runs passed. No serial device was opened for these
  checks; no flash, motor spin, or powered hardware test was performed.
- Generated MAVLink headers produce packed-member alignment warnings during
  compilation. They are not treated as evidence of hardware validation.

## Unpowered PWM diagnostic

`feather_f405_pwm_diagnostic` is for investigating the September 27 stop failure.
**Keep the ESC battery disconnected throughout.** It continuously produces the
same nominal 1000 µs signal on all four pads and rejects all incoming commands
by discarding them. It never runs a throttle test. It identifies itself using
custom mode `0x50574D44` (PWMD), which the motor-test host tool will reject.

The main loop samples GPIOB input bits 8/9 and GPIOC input bits 6/7, corresponding
to Feather 9/10/6/5. It observes rising/falling edges and sends MAVLink
NAMED_VALUE_INT measurements (`M1_US`, `M1_PERIOD`, `M1_MIN`, `M1_MAX`, and
similarly for channels 2–4). A fresh nominal signal should be approximately
1000 µs high with a 20000 µs period. Sampling gaps over 50 µs invalidate the
current pulse measurement; missing or stale widths are reported as -1.

This is a GPIO-pad diagnostic using the MCU's own clock, not an independent
oscilloscope. It can reveal gross pulse-generation errors but does not verify
voltage levels, the physical ESC connection, the ESC protocol/settings, stop
behavior, or motor numbering. The dashboard shows these measurements when the
diagnostic image is running. Do not reconnect motor power based on these
measurements alone.

```sh
pio run -e feather_f405_pwm_diagnostic
```
