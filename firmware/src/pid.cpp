// ============================================================================
//  pid.cpp  --  Single-axis PID controller implementation
// ============================================================================
#include "pid.h"

float Pid::update(float error, float dt) {
    if (dt <= 0.0f) {
        // Guard against a bad/zero timestep: hold output at the P term only,
        // and don't corrupt the integral or derivative state.
        return gains_.kp * error;
    }

    // --- Proportional -------------------------------------------------------
    const float p = gains_.kp * error;

    // --- Integral (with anti-windup clamp) ----------------------------------
    integral_ += error * dt;
    if (iLimit_ > 0.0f) {
        const float iTermMax = iLimit_;
        if (gains_.ki != 0.0f) {
            // Clamp the *contribution* (ki * integral) to +/- iLimit by
            // clamping the accumulator accordingly.
            const float integralMax = iTermMax / gains_.ki;
            if (integral_ >  integralMax) integral_ =  integralMax;
            if (integral_ < -integralMax) integral_ = -integralMax;
        }
    }
    const float i = gains_.ki * integral_;

    // --- Derivative (on error; skipped on the first step to avoid a spike) --
    float d = 0.0f;
    if (havePrev_) {
        d = gains_.kd * (error - prevError_) / dt;
    }
    prevError_ = error;
    havePrev_  = true;

    return p + i + d;
}
