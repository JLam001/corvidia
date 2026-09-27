#pragma once
#include "bench_protocol.h"
#include "dshot_test.h"

namespace DshotProtocol {
// Distinct identity prevents a host expecting a ramp from using legacy output.
constexpr uint32_t RAMP_MS = CORVIDIA_DSHOT_RAMP_MS;
constexpr uint32_t MODE = RAMP_MS ? 0x44534936 : 0x44534935; // DSI6 / DSI5; NOT flight
constexpr uint32_t REVISION = RAMP_MS ? 6 : 5;
constexpr uint16_t ENABLE = MAV_CMD_USER_1, TEST = MAV_CMD_USER_2;
constexpr uint16_t TEST_ALL = MAV_CMD_USER_3;
inline bool integer(float v) { return std::isfinite(v) && v == std::floor(v); }
inline bool token(float v) { return integer(v) && v >= 1 && v <= DshotTest::MAX_TOKEN; }
inline bool stopShape(const mavlink_command_long_t& c) {
    return BenchProtocol::finiteParams(c) && c.param1 == 0 && c.param2 == 0 &&
        c.param3 == 0 && c.param4 == 0 && c.param5 == 0 && c.param6 == 0 && c.param7 == 0;
}
inline bool enableShape(const mavlink_command_long_t& c) {
    return BenchProtocol::finiteParams(c) && c.confirmation == 0 &&
        c.param1 == 1 && token(c.param2) &&
        ((c.param3 == 0 && c.param4 == 0) ||
         (c.param3 > 0 && c.param3 <= 100 && integer(c.param4) &&
          c.param4 >= 20 && c.param4 <= DshotLimits::MAX_DURATION_MS)) &&
        c.param5 == 0 && c.param6 == 0 && c.param7 == 0;
}
inline DshotLimits limits(const mavlink_command_long_t& c) {
    DshotLimits result;
    if (enableShape(c) && c.param3 != 0) {
        result.percent = c.param3; result.durationMs = uint32_t(c.param4);
    }
    return result;
}
// USER_2: motor, throttle percent, duration ms, single-use token, 0, 0, 0.
// Custom command avoids treating DShot units as PWM microseconds or RPM.
inline bool testShape(const mavlink_command_long_t& c) {
    return BenchProtocol::finiteParams(c) && c.confirmation == 0 &&
        integer(c.param1) && c.param1 >= 1 && c.param1 <= 4 &&
        c.param2 > 0 && c.param2 <= 100 && integer(c.param3) &&
        c.param3 >= 20 && c.param3 <= DshotTest::MAX_RUN_MS && token(c.param4) &&
        c.param5 == 0 && c.param6 == 0 && c.param7 == 0;
}
// USER_3: exactly 4 motors, percent, duration ms, token, 0, 0, 0.
// A separate command prevents an invalid USER_2 motor number selecting all.
inline bool allShape(const mavlink_command_long_t& c) {
    return testShape(c) && c.param1 == 4;
}
inline uint16_t throttle(float percent) {
    return std::isfinite(percent) && percent > 0 && percent <= 100 ?
        uint16_t(48 + std::floor(1999 * percent / 100)) : 0;
}
}
