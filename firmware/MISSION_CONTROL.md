# Mission control candidate

## Current result

The software now accepts an operator-selected mission duration and motor ceiling
up to 1.0 (100% normalized command). Nominal collective is a separate setting,
defaulting to 0.5 as requested. It must be below the ceiling to leave room for
attitude corrections. Neither value measures electrical power, RPM or lift.

This is a host-tested candidate, not the final flight firmware. The installed DShot v5 bench image defaults to the verified 5% / 500 ms
envelope and supports larger explicit operator envelopes; see [DSHOT_V5.md](DSHOT_V5.md). No mission packets were transmitted or ESC settings changed during mission
candidate development. The later v5 installation only changes the bench interface. Increasing those bench limits would not produce a flight controller.

## Ownership

| Component | Owns |
| --- | --- |
| Operator | Start/stop authority, mission duration, output ceiling, commissioning |
| LLM | Bounded movement proposals or `stop_mission` |
| Jetson supervisor | Validate proposals, current perception/state checks, session and sequence IDs, timely updates |
| STM32 flight layer | Fresh IMU, body-frame controller, mixer, output bounds and validated flight fallback |

The LLM cannot set throttle, arm, extend a mission or send keepalives through its
intent schema. A deterministic supervisor must re-evaluate state before issuing
each new movement. Repeating cached LLM output with new sequence IDs is not a
valid substitute for that evaluation.

## Implemented software

- `include/mission_session.h`: operator session with immutable duration, nominal
  collective and ceiling. Session IDs cannot repeat within a receiver lifetime.
  No demand exists until a valid movement is accepted.
- `include/mission_control.h`: session → attitude/rate controller → quad mixer.
  Unverified gains/layout, stale IMU or invalid controller output invalidate the
  result and latch a fault. Controller history resets between sessions.
- `include/mission_protocol.h`: candidate MAVLink COMMAND_LONG begin/action
  decoder. It checks target, source role, finite values, exact integers, unused
  fields, bounds, session and action sequence.
- `tools/mission_request.py`: offline packet generator and intent validator.
  It has no serial or execution option.
- `mission-intent.schema.json`: movement or mission-stop intent, without power
  or authority fields. Operator settings live in `mission.example.json`.

### Timing

| Limit | Candidate value |
| --- | --- |
| Mission duration | Operator-selected 1–1,800,000 ms (30-minute software bound) |
| Movement duration | 1–1,000 ms |
| Trusted supervisor lease | 250 ms; suggested update interval 50 ms |
| Tilt / yaw-rate request | At most 10 degrees / 30 degrees per second |

Keepalives do not extend a movement or mission. Once a movement expires, this
candidate ends the session; a late update cannot resume it. During a longer
mission the supervisor must submit new valid movements before the active action
expires. A fresh action does not refresh the independent supervisor lease.

`stop_mission`, deadline expiry or loss of supervision revokes mission authority.
**An invalid motor frame means flight fallback is required; it is not an
instruction to write zeros to airborne motors.** That fallback is not implemented
here. No fixed throttle can be labeled a controlled descent. The legacy arbiter's
fixed 0.30 descent output is not used by this candidate.

## Packet contract (candidate only)

All packets target system/component 1/1 with confirmation 0. IDs and sequence
values are at most 16,777,215 for exact representation in COMMAND_LONG floats.

| Command | Sender | Parameters 1–7 |
| --- | --- | --- |
| USER_5 / 31014 begin | Operator 255/190 | session ID, nominal collective, ceiling, mission duration ms, 0, 0, 0 |
| USER_4 / 31013 action | Supervisor 42/191 | action code, amount, duration ms, session ID, sequence, 0, 0 |

Action codes: level=0, forward tilt=1, backward tilt=2, left tilt=3, right tilt=4,
left yaw=5, right yaw=6. Tilt uses degrees; yaw uses degrees per second.
These are attitude requests, not velocity/position guarantees. “Level” is not hover.

Local `refresh()` and `stop()` operations exist, but their transport handling is
not implemented. Neither installed firmware image dispatches these mission
commands. Session negotiation after reboot, receipt ACKs/status, authenticated
operator authority, supervisor lease transport and airborne fallback must be
finished before connecting this contract to flight outputs. MAVLink source IDs
alone provide no authentication.

MAVLink defines the command container and fields; these USER commands are our
application contract, not standard PX4 flight commands. See the
[MAVLink common definitions](https://mavlink.io/en/messages/common.html#COMMAND_LONG).
PX4's [offboard control documentation](https://docs.px4.io/main/en/flight_modes/offboard)
also distinguishes continuing control updates from link-loss handling; this
project uses its own receiver and must implement its own fallback.

## Run the offline preview

From the repository root:

```sh
.venv-control/bin/python firmware/tools/mission_request.py \
  --settings firmware/mission.example.json \
  --intent '{"action":"tilt_forward","amount":4,"duration_ms":500}'
```

The output includes packet bytes for inspection, `execution_available: false`
and a requested attitude. Example settings are a 60-second mission, nominal
collective 0.5 and motor ceiling 1.0. They are not flight calibration.

## Hardware facts and remaining work

The user confirmed the mounted positions, viewed from above with the camera forward:

```text
                  camera / forward
          4 front-left      2 front-right
          3 rear-left       1 rear-right
```

The user subsequently confirmed the positions remained unchanged and rotation
now matches the selected pattern: 1/4 clockwise, 2/3 counterclockwise, viewed
from above. The method used to correct rotation was not specified. The observed
rotation check is complete; the software candidate records the resulting yaw
signs but keeps flight integration gated pending body-axis and controller checks.
See [the direction record](../docs/references/control/motor-directions-2026-09-27.json).

Before flight-output integration:

1. Rotation and positions are confirmed by the user. Match eventual propeller
   handedness to those directions; keep props removed during current checks.
2. Dominant IMU axes have been measured as body X=−sensor X, Y=+sensor Y,
   Z=−sensor Z; software restoring-direction tests pass. Finish precise mounting
   alignment, absolute attitude conversion and deliberate gyro-motion checks.
   See the [axis-check record](../docs/references/control/imu-alignment/README.md).
3. Tune/validate rate and attitude gains. Test timing, CPU/IMU/link failures and
   independent motor-output deadlines with appropriate measurement and restraint.
4. Implement velocity/height estimation and vertical control. Static 50% collective
   does not hold height. No altitude/velocity feedback is currently connected.
5. Implement and verify pilot override and flight fallback (landing where sensing
   supports it), plus battery monitoring. A pilot-handoff label alone is not a
   working receiver/control path.
6. Complete mission transport and connect validated control to a separately
   supervised output driver. The bench DShot lease is not a mission actuator API.

Validation: all 66 native C++ tests and 35 Python tests pass. Added tests cover
fixed deadlines, stale supervision, action expiry, repeated/out-of-order requests,
session restart, clock wrap, fault latching, mixer/gain gates, role/target checks,
float packet bounds and Python MAVLink encode/decode. No flight validation is claimed.
