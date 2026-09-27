# IMU axis check — 2026-09-27

The operator confirmed ESC battery disconnected, props removed, USB connected
and BNO085 rigidly mounted. The frame could not be made level, so its current
resting pose was used only as a relative reference. No zero/level calibration,
firmware upload, heartbeat or motor command was sent.

The operator confirmed each requested pose before capture. All captures contain
about 50 Hz attitude and quaternion telemetry, IMU_OK=1, IMU_REJ=0,
DS_FAULT=0, DS_ACTIVE=0 and zero requested motor input. DS_TOKEN remained 7.

| Physical motion | Relative sensor rotation vector (degrees) | Dominant axis |
| --- | --- | --- |
| Right side lowered | −23.51, +4.03, −3.03 | −X |
| Front/camera raised | −1.89, +26.83, +2.77 | +Y |
| Camera turned right | −3.09, −3.09, −32.26 | −Z |

Comparisons use conjugate(reference quaternion) × pose quaternion, normalized
and sign-aligned, with the final half of each eight-second capture averaged.
The relative-reference calculation is tested with nonlevel synthetic poses.
The first nose-up and yaw samples failed the three-degree stationary-spread
criterion; both were retained as rejected evidence and repeated. Accepted pose
spreads were approximately 0.70°, 1.50°, and 1.41° respectively.

The candidate sensor-to-body vector map is diag(−1,+1,−1), a proper rotation.
This identifies dominant axes/signs. Hand-positioned cross-axis motion is too
large to claim a precise mounting matrix. An absolute attitude transformation
and a deliberate motion/rate consistency check are still required. Do not simply
negate isolated Euler readings and declare flight calibration complete. The
current firmware still publishes its original sensor-frame attitude and gyro.

## Controller correction signs (software only)

Using the user-confirmed rotor pattern (1/4 CW, 2/3 CCW) and synthetic test gains:

- Right side down: increase right motors 1/2; decrease left motors 3/4.
- Nose up: increase rear motors 1/3; decrease front motors 2/4.
- Turning right: increase CW rotors 1/4; decrease CCW rotors 2/3.

All six mixer/controller unit tests pass, including the new restoring-direction
test for this layout. The three relative-quaternion analysis tests pass. This
checks software signs only; real stabilization gains and powered closed-loop
behavior remain unverified. Flight output stays gated.

See [assessment.json](assessment.json) for computed results and individual JSON
files for full timestamped MAVLink messages.
