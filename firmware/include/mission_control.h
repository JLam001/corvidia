#pragma once
#include "mission_session.h"
#include "attitude_control.h"

// Software evaluation path. No motor driver is included. Invalid results mean
// flight fallback is required, not that zero should be written to the ESCs.
class MissionControl {
public:
    MissionControl(const ControlGains& gains, const QuadLayout& layout)
        : controller_(gains), layout_(layout) {}
    MissionSession& session() { return session_; }
    MotorFrame update(uint32_t now, const BodyState& state, float dt) {
        if (session_.id() != controllerSession_) {
            controller_.reset(); controllerSession_ = session_.id();
        }
        const bool healthy = state.attitudeFresh && state.gyroFresh;
        const auto demand = session_.demand(now, healthy);
        if (!demand.valid) { controller_.reset(); return {}; }
        const auto control = controller_.update(demand.attitude, state, dt);
        if (!control.valid) { session_.update(now, false); return {}; }
        auto motors = mixQuadX(layout_, demand.collective, control.effort, demand.ceiling);
        if (!motors.valid) { session_.update(now, false); controller_.reset(); }
        return motors;
    }
private:
    MissionSession session_;
    AttitudeController controller_;
    QuadLayout layout_;
    uint32_t controllerSession_ = 0;
};
