# DShot commissioning — 2026-09-27

Installed image: integrated IMU/individual/all-four v4, 59,740-byte payload;
see [installation record](dshot-v4-install-2026-09-27.md).

## Preconditions

User previously confirmed props removed, all four motors secured, signal rewiring
completed and T disconnected. Before the test the user confirmed battery connected,
startup finished, all motors still and hands clear. No reflash was performed.

The DShot host now requires explicit per-invocation readiness and fresh zero input
on all four channels before enabling. All-four execution requires individual stop
observations. Legacy PWM remains blocked. All 30 host tests pass, including missing
readiness, nonzero initial input, all-four prerequisites and lost-ACK stop behavior.

## Test 1

- Channel: 1 (Feather 9/PB8; planned rear-right).
- Input: 1%, DShot value 67. RPM is unknown.
- Requested duration: 200 ms, bounded by the installed firmware.
- Start/test and explicit zero request acknowledged; no retry.
- Operator observation: only motor 1 spun smoothly, then stopped; all motors still.
- Post-test capture: all requested inputs zero, DS_ACTIVE=0, DS_FAULT=0,
  IMU_OK=1, DS_TOKEN=2; 749 attitude and quaternion packets each in 15 seconds.
  DS_EXPIRE=1 records lease expiration; the specific expiration cause is not reported.
  [Capture](dshot-motor1-after-test-2026-09-27.json).

Read-only post-test telemetry is saved separately. Signal timing, actual
RPM and physical stopping cannot be inferred from firmware telemetry or ACKs.

## Test 2

- Channel: 2 (Feather 10/PB9; planned front-right).
- Input: 1%, DShot value 67; requested duration 200 ms.
- Test and zero request acknowledged; no retry.
- Operator observation: only motor 2 spun smoothly, then stopped; all motors still.
- No simultaneous command sent.

## Test 3

- Channel: 3 (Feather 6/PC6; planned rear-left).
- Input: 1%, DShot value 67; requested duration 200 ms.
- Test and zero request acknowledged; no retry.
- Operator observation: only motor 3 spun smoothly, then stopped; all motors still.

## Test 4

- Channel: 4 (Feather 5/PC7; planned front-left).
- Input: 1%, DShot value 67; requested duration 200 ms.
- Test and zero request acknowledged; no retry.
- Operator observation: only motor 4 spun smoothly, then stopped; all motors still.

## Test 5 — simultaneous all-four

- Sent after all four individual physical motion/stop observations passed.
- Explicit USER_3 all-four command, 1% input (DShot 67), 200 ms.
- Test and zero request acknowledged; no retry.
- Operator confirmed all four spun smoothly and stopped; every motor still.
- Asked operator to unplug ESC battery after completing tests.

These observations validate these five short bench pulses only. RPM, waveform
timing, extended runtime, fault injection, load behavior and flight control remain
unverified. No firmware was reflashed during these tests.

## Final telemetry and requested escalation

The 15-second [post-test capture](dshot-all-after-test-2026-09-27.json)
contains 749 ATTITUDE and 749 quaternion packets, no bad fragments, IMU_OK=1,
DS_FAULT=0, DS_ACTIVE=0 and M1_DS through M4_DS=0. DS_TOKEN=6 is consistent
with five consumed enables. DS_EXPIRE=1 does not identify the expiration cause.

The user subsequently requested all four at 50% for five seconds. No such command
was sent; it exceeds both installed limits (5%, 500 ms). Those limits were retained.

## Test 6 — simultaneous all-four at existing limits

- User requested 5% input for 500 ms and reconfirmed props removed, motors
  secured, battery connected, startup stationary and hands/cables clear.
- Sent one explicit all-four USER_3 request: 5%, DShot value 147, 500 ms.
- Test and zero request acknowledged; no retry. No firmware limits changed.
- Operator confirmed all four spun smoothly and stopped; every motor still.
- Post-test telemetry: all four requested inputs zero, DS_ACTIVE=0, DS_FAULT=0,
  IMU_OK=1, DS_TOKEN=7, no bad fragments; 749 attitude and 749 quaternion
  packets in 15 seconds. RPM remains unmeasured.
- [Post-test capture](dshot-all-5pct-500ms-after-test-2026-09-27.json).

## Test 7 — v5, all four at 5% for one second

After v5 was flashed/readback-verified and its unpowered startup checked, the
operator confirmed props off, all motors secured, battery connected, startup
finished, all motors still and hands/cables clear. Sent one all-four request at
5% input (DShot 147), 1000 ms, with an explicit 5%/1000 ms envelope. Test/stop
were acknowledged; fresh post-stop reports showed DS_ACTIVE=0, DS_FAULT=0 and
all four requested inputs zero. No retry. The user confirmed all four spun smoothly for the pulse and stopped;
every motor still. The post-test capture also showed default limits restored
(5%/500 ms), DS_TOKEN=2 and healthy IMU telemetry.
[Capture](dshot-v5-after-5pct-1000ms-2026-09-27.json).

## Test 8 — v5, all four at 7.5% for one second

After the user confirmed test 7 stopped, sent one all-four request at 7.5% input
(DShot 197), 1000 ms, with an explicit 7.5%/1000 ms envelope. Test/stop
acknowledged; fresh post-stop reports confirmed idle, no output fault and zero
requested inputs. No retry. The user confirmed all four ran smoothly and stopped;
every motor still. [Post-test capture](dshot-v5-after-7p5pct-1000ms-2026-09-27.json)
shows healthy IMU telemetry, no output fault, all inputs zero, DS_TOKEN=3 and the
default 5%/500 ms envelope restored. Asked the user to unplug the ESC battery
after completing this session.

## Test 9 — v5, all four at 50% for 200 ms

The user explicitly requested this pulse and reconfirmed props off, motors firmly
secured, battery connected, startup stationary and hands/cables clear. Sent one
all-four request at 50% input (DShot 1047), 200 ms, with an explicit 50%/200 ms
envelope. Test and stop acknowledged; fresh zero-input and fault-free idle reports
confirmed by the host. No retry. The user confirmed all four ran smoothly and
stopped; every motor still. Asked the user to unplug the ESC battery afterward.
[Post-test capture](dshot-v5-after-50pct-200ms-2026-09-27.json).

## Test 10 — v5, all four at 50% for 2000 ms

The user requested the longer pulse and reconfirmed props off, motors firmly
secured, battery connected, startup stationary and hands/cables clear. Sent one
all-four request at 50% input (DShot 1047), 2000 ms, with an explicit 50%/2000 ms
envelope. Test and stop acknowledged; fresh zero-input and fault-free idle reports
confirmed by the host. No retry. The user confirmed all four ran smoothly and
stopped; every motor still.

Post-test telemetry for test 10: all motor inputs zero, DS_ACTIVE=0, DS_FAULT=0,
IMU_OK=1, IMU_REJ=0, DS_TOKEN=5 and default 5%/500 ms limits restored.
[Capture](dshot-v5-after-50pct-2000ms-2026-09-27.json). The user confirmed physical stopping separately; telemetry does not measure RPM.

## Test 11 — final requested test, v5 all four at 20% for 10 seconds

The user requested this final test and reconfirmed props off, motors firmly
secured, battery connected, startup stationary and hands/cables clear. Sent one
all-four request at 20% input (DShot 447), 10000 ms, with an explicit 20%/10000 ms
envelope. Test and stop acknowledged; fresh zero-input and fault-free idle reports
confirmed by the host. No retry. The user confirmed all four ran smoothly
throughout the full pulse and stopped; every motor still.

Final [15-second capture](dshot-v5-after-20pct-10000ms-2026-09-27.json):
749 attitude and 749 quaternion reports, zero bad fragments, IMU_OK=1, IMU_REJ=0,
DS_FAULT=0, DS_ACTIVE=0, all motor inputs zero, DS_TOKEN=6, and default 5%/500 ms
envelope restored. ESC battery disconnection requested; visualizer restarted.
No more tests were sent after this final requested run. This does not validate
flight, maximum throttle, maximum duration, or behavior with propellers fitted.
