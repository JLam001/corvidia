# DShot IMU operator-envelope v5 — September 27, 2026

Status: flashed and boot-verified, with all 60,356 payload bytes matching
readback. ESC battery remained disconnected during startup verification.

The user requested an integrated testing image with selectable power and timing,
then specified a maximum fixed duration of one minute for stand demos. No
autonomous-flight readiness is claimed. Defaults remain 5% / 500 ms; supported
explicit envelopes extend to 100% / 60,000 ms.

- Profile: `feather_f405_dshot_imu_bench`.
- New identity: `0x44534935`, `FW_REV=5`.
- Payload: 60,356 bytes.
- Payload SHA256: `87c9e755933a3fb99ad9016cecad8e66a3261b6400887ee6f1030f1b68069957`.
- DFU-suffixed file SHA256: `ed4ad4b1e56f6767d24a4192f2ad821ea6501bc479442877ddc209b965ab1d19`.
- Preserved image: `~/Documents/Corvidia-hardware-backups/2026-09-27/dshot-imu-operator-envelope-v5.bin`.
- Existing v4 image/readback and original 1 MB backups remain preserved.
- Validation: 70 C++ tests and 43 Python tests passed, dashboard JavaScript syntax
  checked, all three integrated firmware profiles built successfully.
- No nonzero motor command has been sent during v5 preparation.

See [the v5 interface](../../../firmware/DSHOT_V5.md).

The user confirmed DFU ready, ESC battery unplugged and T disconnected. A full
1 MB pre-flash backup was saved as `stm32-before-v5.bin`; its first 59,740 bytes
match the preserved v4 firmware exactly. Backup SHA256:
`274c26d3e8a492cb24cf4b726deb390d4d444ffd71fa9135e52a7db6ff69b7ac`.
Only internal-flash alt 0 was written; option bytes and OTP were not changed.

## Live startup verification

The [15-second capture](dshot-v5-startup-2026-09-27.json) received 749 ATTITUDE
and 749 quaternion messages, 579 named values, 20 heartbeats and zero bad
fragments. Identity is DSI5, FW_REV=5. IMU_OK=1, IMU_REJ=0, attitude/gyro ages
4/1 ms. DS_FAULT=0, DS_ACTIVE=0 and all four requested inputs zero.
DS_MAXP=100, DS_MAXMS=60000; the current envelope is DS_CAP_BP=500 (5%)
and DS_CAP_MS=500. FLT_READY=0, AXES_OK=0. A stop-only request was acknowledged
after capture. No nonzero command was sent during installation or this check.

## Initial powered commissioning

After startup verification, the user confirmed props off, motors secured, ESC
battery connected, startup finished with all motors still and hands/cables clear.
One all-four test at 5% for 1000 ms passed, followed by one at 7.5% for 1000 ms.
The user confirmed smooth rotation and complete stop after both. Each command
and stop was acknowledged; fresh zero-input telemetry was checked. No retries.
Final 15-second telemetry confirms IMU_OK=1, IMU_REJ=0, DS_FAULT=0, DS_ACTIVE=0,
all motor inputs zero, DS_TOKEN=3, and defaults restored (5%/500 ms).
These tests do not validate full power or a 60-second run. The battery was
requested disconnected after the tests. See the [powered session record](dshot-powered-tests-2026-09-27.md).

A subsequent user-requested all-four pulse at 50% for 200 ms was sent once after
fresh physical readiness confirmation. The user confirmed smooth rotation and
complete stop. Test/stop ACKs and fresh zero-input telemetry were received. This
short pulse does not validate sustained 50% operation or the maximum duration.

The next user-requested all-four test at 50% for 2000 ms also passed operator-
observed smooth rotation and complete stop. The host confirmed test/stop ACKs
and fresh zero inputs; final telemetry reports no output fault, healthy IMU,
DS_TOKEN=5 and default limits restored. No longer run is validated by this test.

Final requested commissioning run: all four at 20% for 10,000 ms. The user
confirmed smooth running for the full pulse and complete stop. Host ACKs and
fresh zero-input checks passed. Final telemetry: IMU healthy, no output fault,
all motor inputs zero, DS_TOKEN=6 and default envelope restored.
