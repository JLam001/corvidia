#pragma once
#include "command_arbiter.h"
#include "mavlink/common/mavlink.h"

// Bridge MAVLink <-> our neutral Setpoint. Kept free of Arduino headers so the
// field mapping is host-unit-testable (see test_mavlink_roundtrip).

// Decode a SET_ATTITUDE_TARGET message (the Jetson's down-link command) into a
// Setpoint. We take the body rates + collective thrust; whether the axes are
// interpreted as angles or rates is the controller's decision in Phase 2.
inline Setpoint decodeSetpoint(const mavlink_message_t& m) {
    mavlink_set_attitude_target_t t;
    mavlink_msg_set_attitude_target_decode(&m, &t);
    Setpoint sp;
    sp.roll   = t.body_roll_rate;
    sp.pitch  = t.body_pitch_rate;
    sp.yaw    = t.body_yaw_rate;
    sp.thrust = t.thrust;
    return sp;
}
