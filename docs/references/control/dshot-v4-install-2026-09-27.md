# DShot IMU bench v4 installation — September 27, 2026

The user requested individual and simultaneous all-four tests in one firmware
image, so passing the individual tests would not require another flash.

- Device: Feather STM32F405, USB serial `326738693533`.
- Profile: `feather_f405_dshot_imu_bench`.
- ESC battery disconnected; no motor command sent during installation.
- Payload: 59,740 bytes; full payload readback matches byte-for-byte.
- Payload SHA256: `18c85b8bcef311b02ff5a52f9f07f4b06c7bec3aea66aeca496cf4c279222eb1`.
- Build-file SHA256 including DFU suffix: `31023c7564f6cb471bdeaad10e5c4be9509b9a53ca8bf67b9202d445ca5c228b`.
- Backup/image/readback/manifest are stored in
  `~/Documents/Corvidia-hardware-backups/2026-09-27/`.
- Original installed firmware remains preserved in two matching 1 MB backups.

V4 includes the report-sequence freshness fix, individual USER_2 and explicit
all-four USER_3 commands. Both use the same enable-token, finite-duration,
heartbeat, IMU and main-loop deadlines. Startup does not automatically enable a
test. All-four capability is compiled into this image and needs no later reflash.

Validation before upload: three integrated profiles built successfully, 57 native
C++ tests passed, and 28 Python tests passed. Physical DShot waveforms, ESC
acceptance, RPM and motor-stop behavior are not established by those tests.

## Live check

The board booted after jumper removal and RESET. A 15-second read-only capture
received 749 ATTITUDE and 749 ATTITUDE_QUATERNION packets, about 50 Hz each.
The IMU is at 0x4A, configured, with fresh attitude/gyro ages of 2/1 ms and zero
rejected reports. DShot reports no DMA fault; both DS_ALLOW and DS_ALL are 1.
DS_ACTIVE and all four requested motor inputs are zero. A stop-only command was
acknowledged afterward. ESC battery remained disconnected throughout.

Evidence: [live capture](dshot-imu-bench-v4-live-2026-09-27.json).
No nonzero motor command or powered motor test was performed in this check.
Physical waveforms, RPM and stopping still require verification.
