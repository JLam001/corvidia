# STM32 bench session — September 27, 2026

## Confirmed by the user

- Props removed; all four motors mounted or clamped.
- ESC signal 3 moved from Feather 11 to 6; ESC signal 4 moved from 12 to 5.
- Battery and ESC T-to-RX disconnected before entering DFU.

## Observed over USB

The installed image initially streamed MAVLink from system/component 1/1:
ATTITUDE around 50 Hz, ATTITUDE_QUATERNION, HEARTBEAT, SYS_STATUS,
SERVO_OUTPUT_RAW, ATTITUDE_TARGET, EXTENDED_SYS_STATE, and SYSTEM_TIME.
The heartbeat reported disarmed. Sensor-status bits reported gyro and
accelerometer present, enabled and healthy. Reported outputs 1–4 were 1000 µs.
Attitude and gyro values changed when the user moved the sensor. This verifies
live response, not sensor-to-airframe alignment or closed-loop stabilization.
The installed image differs from the imported source.

## Firmware backup and bench upload

Full internal flash (1,048,576 bytes) was read twice and compared byte for byte.
Backup outside the repository:
`~/Documents/Corvidia-hardware-backups/2026-09-27/stm32-before-motor-bench.bin`

SHA256: `b0bda832a336dae908bcab94d02045420b4f8d758139307c4378a91c7b3f69d7`.

The `feather_f405_bench_pwm` image was programmed and its 36,464-byte payload
verified by readback. It was programmed again with the DFU `leave` option after
RESET left the device in DFU; the tool reported a successful download and leave.
The application then reported the expected BENC custom mode (1111838275),
STANDBY state, disarmed. An explicit MAVLink stop command was acknowledged.
No flight arm command was sent.

Payload SHA256: `bca66a9a2c9deea7e0d600f9d69e4396b8d4e058d41cb325b720894c252beb4a`.

## Powered test

User confirmed ESC startup tones completed with every motor stationary.
Channel 1 received a 1020 µs request for 0.5 seconds. The firmware acknowledged
the test and subsequent stop. The user initially reported that the intended motor twitched and stopped by
itself. A second stop was sent and acknowledged. A proposed 1040 µs test has
NOT been sent. Before that test, the user reported “its spinning smoothly right
now” outside an active commanded test. Another stop was immediately sent and
acknowledged; the user was instructed to disconnect ESC power. An asynchronous
“ready” reply to the earlier test prompt does not resolve this discrepancy.
The user then confirmed motor 4 was still spinning with the battery connected.
After a direct instruction to unplug the battery, the user confirmed the battery
was unplugged and all motors stopped. Powered testing remains stopped.
The battery-disconnected PWM-pad diagnostic and subsequent original-firmware
restoration are recorded below. No 1040 µs test was sent. Actual motor RPM was
not measured.

## Visualizer

`firmware/tools/imu_viz.py` now reads binary MAVLink instead of the old ASCII
format. Local HTTP/API and 17 host tests passed; automatic browser inspection
was denied by browser permissions, so rendered appearance has not been verified.

## Unpowered pad measurements

The diagnostic image was flashed and its 33,704-byte payload verified by DFU
readback, then programmed with a leave request to boot. Payload SHA256:
`f99c7383edbc85eefa22921dc892a98738935a1b8095629bbfdb4b0d13846c6b`.
The ESC battery remained disconnected, as confirmed by the user.

MAVLink identified PWMD mode and delivered all 16 named measurements. All four
pads measured approximately 1000 µs high / 20000 µs period. Early observed
width ranges were 986–1012 µs on channels 1/2 and 987–1013 µs on channels 3/4;
these include software-polling uncertainty and must not be described as measured
hardware jitter. Full snapshot: `pwm-diagnostic-2026-09-27.json`.

These observations show no gross timing error in the shared PWM output code
under the fixed diagnostic condition. They do not reproduce the powered incident
or establish its cause. Actual ESC channel routing and ESC protocol/throttle
endpoints still require verification. Do not run another powered test until
stop behavior has been established.

The host motor tool now blocks spin requests before opening the serial port.
Dry runs and explicit stop remain available. There is no CLI bypass. All 18 host
tests pass. The full original 1 MB firmware has been restored and the complete readback
matches the saved backup byte for byte. After the user removed BOOT0 and reset,
MAVLink ATTITUDE returned at 50 Hz with the original message set, disarmed
heartbeat and no decode errors in the observed sample. Snapshot:
`restored-imu-2026-09-27.json`. The ESC battery remains disconnected.


Final state: original firmware restored, live IMU dashboard running at
http://127.0.0.1:8765 using the repository's `.venv-control` environment. Further
spin requests are blocked in `motor_test.py`. Cause of the sustained motor-4
rotation is unresolved; inspect actual signal routing and ESC model/configuration
before deciding how to establish a verified stop state.

The user subsequently identified the ESC as the Flywoo GOKU G45M AM32.
See [the model investigation](ESC-G45M-AM32.md). This identifies the product,
but does not resolve the stop failure or authorize resuming powered tests.
