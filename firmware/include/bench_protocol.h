#pragma once
#include <cmath>
#include "motor_test.h"
#include "mavlink/common/mavlink.h"

namespace BenchProtocol {
constexpr uint8_t SYSTEM = 1, COMPONENT = MAV_COMP_ID_AUTOPILOT1;
constexpr uint8_t OPERATOR_SYSTEM = 255, OPERATOR_COMPONENT = MAV_COMP_ID_MISSIONPLANNER;
constexpr uint32_t MODE_ID = 0x42454E43; // "BENC": explicitly identifies bench firmware
constexpr uint16_t ENABLE_COMMAND = MAV_CMD_USER_1;

inline bool fromOperator(const mavlink_message_t& msg) {
    return msg.sysid == OPERATOR_SYSTEM && msg.compid == OPERATOR_COMPONENT;
}
inline bool addressed(const mavlink_command_long_t& c) {
    return c.target_system == SYSTEM && c.target_component == COMPONENT;
}
inline bool finiteParams(const mavlink_command_long_t& c) {
    return std::isfinite(c.param1) && std::isfinite(c.param2) &&
           std::isfinite(c.param3) && std::isfinite(c.param4) &&
           std::isfinite(c.param5) && std::isfinite(c.param6) && std::isfinite(c.param7);
}
inline bool enableShape(const mavlink_command_long_t& c) {
    return finiteParams(c) && (c.param1 == 0 || c.param1 == 1) &&
           c.param2 == 0 && c.param3 == 0 && c.param4 == 0 &&
           c.param5 == 0 && c.param6 == 0 && c.param7 == 0;
}
inline bool motorShape(const mavlink_command_long_t& c) {
    return finiteParams(c) && c.param1 >= 1 && c.param1 <= 4 &&
        c.param1 == std::floor(c.param1) && c.param2 == MOTOR_TEST_THROTTLE_PWM &&
        c.param3 >= MotorTest::STOP_US && c.param3 <= MotorTest::MAX_US &&
        c.param3 == std::floor(c.param3) && c.param4 >= 0.02f && c.param4 <= 2 &&
        c.param5 == 1 && c.param6 == MOTOR_TEST_ORDER_BOARD && c.param7 == 0;
}
}
