#pragma once
#include "mission_session.h"
#include "mavlink/common/mavlink.h"

// Candidate wire contract, NOT dispatched by any installed firmware image.
// Source IDs distinguish roles on a trusted link; they are not authentication.
namespace MissionProtocol {
constexpr uint16_t ACTION = MAV_CMD_USER_4, BEGIN = MAV_CMD_USER_5;
constexpr uint8_t SYSTEM = 1, COMPONENT = 1;
constexpr uint8_t OPERATOR_SYSTEM = 255, OPERATOR_COMPONENT = 190;
constexpr uint8_t SUPERVISOR_SYSTEM = 42, SUPERVISOR_COMPONENT = 191;
constexpr uint32_t MAX_ID = 16777215; // COMMAND_LONG float32 exact integer range

inline bool integer(float n, uint32_t lo, uint32_t hi) {
    return std::isfinite(n) && n >= lo && n <= hi && std::floor(n) == n;
}
inline bool receive(MissionSession& session, const mavlink_message_t& msg,
                    uint32_t now, bool stateHealthy) {
    if (msg.msgid != MAVLINK_MSG_ID_COMMAND_LONG) return false;
    mavlink_command_long_t c;
    mavlink_msg_command_long_decode(&msg, &c);
    if (c.target_system != SYSTEM || c.target_component != COMPONENT || c.confirmation != 0)
        return false;
    const float fields[] = {c.param1,c.param2,c.param3,c.param4,c.param5,c.param6,c.param7};
    for (float f:fields) if (!std::isfinite(f)) return false;
    if (c.command == BEGIN) {
        if (msg.sysid != OPERATOR_SYSTEM || msg.compid != OPERATOR_COMPONENT ||
            !integer(c.param1,1,MAX_ID) || !integer(c.param4,1,1800000) ||
            c.param5 != 0 || c.param6 != 0 || c.param7 != 0) return false;
        MissionSettings p;
        p.nominalCollective=c.param2; p.motorCeiling=c.param3; p.durationMs=uint32_t(c.param4);
        return session.begin(now,uint32_t(c.param1),p,stateHealthy);
    }
    if (c.command != ACTION || msg.sysid != SUPERVISOR_SYSTEM ||
        msg.compid != SUPERVISOR_COMPONENT || !integer(c.param1,0,6) ||
        !integer(c.param3,1,MotionSupervisor::MAX_DURATION_MS) ||
        !integer(c.param4,1,MAX_ID) || !integer(c.param5,0,MAX_ID) ||
        c.param6 != 0 || c.param7 != 0) return false;
    return session.submit(now,uint32_t(c.param4),
        {static_cast<MotionAction>(uint8_t(c.param1)),c.param2,uint32_t(c.param3),uint32_t(c.param5)},
        stateHealthy);
}
// Refresh and stop are local trusted-supervisor operations in this candidate.
// A transport handler for them, reboot/session negotiation, authentication and
// a validated airborne fallback are required before motor-output integration.
}
