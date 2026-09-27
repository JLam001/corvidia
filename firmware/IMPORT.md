# STM32 firmware import

Imported September 27, 2026 from `Downloads/firmware.zip`.

This document records the original import. Subsequent motor-control work and
updated build verification are documented in [MOTOR_CONTROL.md](MOTOR_CONTROL.md).

Archive SHA-256:
`e570d3e84341beb07cd30e58c8f743a515f548436511a1adefb289b64aed8221`

## Included

- PlatformIO project for the Adafruit Feather STM32F405 using STM32duino.
- BNO085 polling: rotation vector and calibrated gyro, requested at 200 Hz.
- USB CDC MAVLink transport: 1 Hz heartbeat and 50 Hz attitude/active-setpoint telemetry.
- Heartbeat watchdog, command arbiter, PID module, and rate timer.
- Four native Unity test suites and the original IMU visualization utility.
- Vendored MAVLink headers and the original editor extension recommendations.

At import, firmware source, configuration, headers, tests, and tools were copied unchanged.
The archive's `.pio/` build/dependency cache and macOS `__MACOSX` metadata are
excluded. The source archive remains in Downloads.

## Current behavior and documentation differences

The original README describes Phase 0 text output; `src/main.cpp` contains the
later MAVLink implementation. **USB CDC now carries binary MAVLink**, so the
README's sample text output and `tools/imu_viz.py` text parser do not describe
the current wire protocol. The referenced `docs/ARCHITECTURE.md` and Jetson
`mavlink.monitor` module were not supplied with this archive or the merged
perception branch.

The code polls the IMU and selects/echoes command setpoints. It does not yet
connect the PID to a motor mixer or implement ESC outputs. The arbiter's fixed
0.30 thrust fallback is a software setpoint, not a demonstrated descent behavior.

The imported driver requests `SH2_ROTATION_VECTOR`, not the game rotation vector
proposed in the Scout architecture. IMU health uses one combined freshness
timestamp for attitude and gyro. These are existing implementation details;
the import does not change the control or sensor behavior.

## Import verification

All 17 existing native test cases passed on macOS using Clang and the Unity
sources supplied in the archive, with empty `setUp`/`tearDown` hooks:

| Suite | Passed |
| --- | ---: |
| Command arbiter | 6 |
| Link watchdog | 3 |
| MAVLink round trip | 4 |
| Rate timer | 4 |

PlatformIO and the ARM compiler were not installed in this environment, so an
STM32 target build and hardware/IMU verification were not performed. With
PlatformIO installed, the project's intended commands from `firmware/` are:

```sh
pio test -e native
pio run -e feather_f405
```

No firmware was flashed during this import.
