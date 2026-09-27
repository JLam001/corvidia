#pragma once
#include <cmath>
#include "motion.h"
#include "quad_mixer.h"

// Software-only controller for simulation and gain evaluation. No ESC access.
// Measurements must already be transformed into body forward/right/down axes.
struct BodyState {
    float rollDeg = 0, pitchDeg = 0;
    float rollDps = 0, pitchDps = 0, yawDps = 0;
    bool attitudeFresh = false, gyroFresh = false;
};
struct ControlGains {
    float angleP = 0;
    float rateP[3] = {0, 0, 0};
    float rateI[3] = {0, 0, 0};
    float rateD[3] = {0, 0, 0};
    float maxRateDps = 60;
    float maxEffort = 0.15f;
    float maxIntegral = 0.03f;
    bool verified = false;
};
struct ControlResult { AxisEffort effort{}; bool valid = false; };

class AttitudeController {
public:
    explicit AttitudeController(const ControlGains& gains) : gains_(gains) {}
    void reset() {
        for (unsigned i = 0; i < 3; ++i) { integral_[i] = previousRate_[i] = 0; }
        havePrevious_ = false;
    }
    ControlResult update(const AttitudeDemand& d, const BodyState& s, float dt) {
        if (!validGains() || !s.attitudeFresh || !s.gyroFresh ||
            !std::isfinite(dt) || dt < 0.001f || dt > 0.02f ||
            !std::isfinite(d.rollDeg) || !std::isfinite(d.pitchDeg) ||
            !std::isfinite(d.yawRateDps) || !std::isfinite(s.rollDeg) ||
            !std::isfinite(s.pitchDeg) || !std::isfinite(s.rollDps) ||
            !std::isfinite(s.pitchDps) || !std::isfinite(s.yawDps) ||
            std::fabs(s.rollDps) > 2000 || std::fabs(s.pitchDps) > 2000 ||
            std::fabs(s.yawDps) > 2000 ||
            std::fabs(d.rollDeg) > MotionSupervisor::MAX_TILT_DEG ||
            std::fabs(d.pitchDeg) > MotionSupervisor::MAX_TILT_DEG ||
            std::fabs(d.yawRateDps) > MotionSupervisor::MAX_YAW_DPS ||
            std::fabs(s.rollDeg) > 45 || std::fabs(s.pitchDeg) > 45) {
            reset(); return {};
        }
        const float target[] = {
            clamp(gains_.angleP * (d.rollDeg - s.rollDeg), gains_.maxRateDps),
            clamp(gains_.angleP * (d.pitchDeg - s.pitchDeg), gains_.maxRateDps),
            d.yawRateDps
        };
        const float measured[] = {s.rollDps, s.pitchDps, s.yawDps};
        float out[3];
        for (unsigned i = 0; i < 3; ++i) {
            const float e = target[i] - measured[i];
            const float p = gains_.rateP[i] * e;
            const float derivative = havePrevious_ ?
                -gains_.rateD[i] * (measured[i] - previousRate_[i]) / dt : 0;
            const float candidate = clamp(integral_[i] + gains_.rateI[i] * e * dt,
                                          gains_.maxIntegral);
            const float proposed = p + candidate + derivative;
            // Don't accumulate I in the direction of an already saturated axis.
            if ((proposed <= gains_.maxEffort || e < 0) &&
                (proposed >= -gains_.maxEffort || e > 0)) integral_[i] = candidate;
            out[i] = clamp(p + integral_[i] + derivative, gains_.maxEffort);
            if (!std::isfinite(out[i])) { reset(); return {}; }
            previousRate_[i] = measured[i];
        }
        havePrevious_ = true;
        ControlResult result;
        result.effort = {out[0], out[1], out[2]};
        result.valid = true;
        return result;
    }
private:
    static float clamp(float x, float limit) { return std::fmax(-limit, std::fmin(limit, x)); }
    bool validGains() const {
        if (!gains_.verified || !std::isfinite(gains_.angleP) || gains_.angleP <= 0 ||
            gains_.angleP > 20 || !std::isfinite(gains_.maxRateDps) ||
            gains_.maxRateDps <= 0 || gains_.maxRateDps > 180 ||
            !std::isfinite(gains_.maxEffort) || gains_.maxEffort <= 0 || gains_.maxEffort > 1 ||
            !std::isfinite(gains_.maxIntegral) || gains_.maxIntegral < 0 ||
            gains_.maxIntegral > gains_.maxEffort) return false;
        for (unsigned i = 0; i < 3; ++i)
            if (!std::isfinite(gains_.rateP[i]) || gains_.rateP[i] <= 0 || gains_.rateP[i] > 1 ||
                !std::isfinite(gains_.rateI[i]) || gains_.rateI[i] < 0 || gains_.rateI[i] > 1 ||
                !std::isfinite(gains_.rateD[i]) || gains_.rateD[i] < 0 || gains_.rateD[i] > 1)
                return false;
        return true;
    }
    ControlGains gains_;
    float integral_[3] = {0, 0, 0}, previousRate_[3] = {0, 0, 0};
    bool havePrevious_ = false;
};
