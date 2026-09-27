# DShot + IMU v6: fixed five-second startup ramp

**Status: deferred, not installed or verified with powered motors.** This
document describes a prepared, opt-in firmware profile. The STM32 and the
active Jetson mission adapter remain on v5, which has immediate input rather
than this ramp. The prepared v6 host changes were archived outside this
checkout; building this profile does not enable v6 support in the current app.

The `feather_f405_dshot_imu_ramp` profile extends the bounded v5 bench image
with a fixed startup ramp. It identifies itself as `DSI6` (`0x44534936`),
reports `FW_REV=6`, and advertises `DS_RAMPMS=5000` over MAVLink.

Every accepted individual or all-four run starts at zero input and rises to
the requested target over five seconds. The ramp uses only valid throttle
codes 48–2047; it never emits ESC setting commands 1–47. At a 25% target,
the commanded values are approximately:

| Time after start | DShot input |
| --- | --- |
| 0 | 0 |
| 1.25 seconds | 172 |
| 2.5 seconds | 297 |
| 3.75 seconds | 422 |
| 5 seconds onward | 547 |

These values describe requested motor input, not measured RPM, current, or
power. A ramp does not establish that a power supply can sustain the final load.

## Timing and stopping

The five seconds are included in the original run duration. A 30-second mission
ramps for five seconds and can hold its target for the remaining 25 seconds.
A shorter run can end before reaching the target. Confirmed first-match
missions can still end during the ramp.

Stop, expiry, main-loop timeout, IMU timeout, and heartbeat timeout bypass the
ramp and command zero immediately. The timer interrupt checks these limits
before calculating ramp progress. Late refreshes cannot revive an expired run;
duplicate starts cannot restart the ramp or extend its deadline.

## Protocol and compatibility

The v5 enable/start/stop packet shapes and single-use tokens are unchanged.
There is no packet field to disable or shorten the ramp. A corresponding v6
host adapter must require the v6 identity, revision, and fresh
`DS_RAMPMS=5000` before enabling hardware. That adapter was prepared but is
not active: the current Jetson mission adapter requires the v5 identity and
rejects v6.

`feather_f405_dshot_imu_bench` remains the installed v5 profile with immediate
input. Do not assume a startup ramp when using that profile. Activating v6
requires a coordinated host update, firmware installation, and separate
verification; none of these steps happens when committing or building the code.
The new profile selects `CORVIDIA_DSHOT_RAMP_MS=5000`; only values 0 and 5000
are supported at compile time.

## Build and verification

```sh
pio test -d firmware -e native -e native_ramp
pio run -d firmware -e feather_f405_dshot_imu_ramp
```

The prepared v6 firmware passed 92 C++ tests. The 715 Jetson tests were run
against the prepared v6 host tree, not the current v5 host checkout. After
restoring the v5 host, its focused validation passed 197 tests. These results
do not establish v6 installation or powered behavior.

The prepared payload is 60,436 bytes with SHA256
`5c4ae7fe919db31f53eb0b3ac0ec7124a84e0b16bdb118db9969821d4946a3fc`.
The DFU-suffixed file is 60,452 bytes. Installation and powered verification
must be recorded separately; successful tests alone do not establish either.
