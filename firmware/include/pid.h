// ============================================================================
//  pid.h  --  Single-axis PID controller (the "control" module building block)
//
//  This is the core math of the flight controller's stabilization: given an
//  error (where we are vs. where we want to be), produce a correction output.
//  The cascaded controller described in ARCHITECTURE.md (§6) is built from
//  several of these -- an inner RATE loop (always active) and an outer ANGLE
//  loop (Level mode) per axis (roll/pitch/yaw).
//
//  Design notes:
//    - Header + .cpp, free of Arduino dependencies, so it can be unit-tested on
//      a host (see test/). Time is passed in as `dt` seconds; the caller owns
//      the clock (the flight loop runs at a fixed rate -- Phase 2 target 500 Hz).
//    - Integral WINDUP is the classic PID hazard on a quad (a saturated output
//      keeps accumulating I, then overshoots badly). We clamp the integral term
//      to +/- iLimit.
//    - reset() must be called on (re)arm so stale integral/derivative state from
//      a previous flight doesn't kick the motors.
// ============================================================================
#pragma once

struct PidGains {
    float kp = 0.0f;
    float ki = 0.0f;
    float kd = 0.0f;
};

class Pid {
public:
    Pid() = default;
    explicit Pid(const PidGains &g, float iLimit = 0.0f)
        : gains_(g), iLimit_(iLimit) {}

    void setGains(const PidGains &g) { gains_ = g; }
    PidGains gains() const { return gains_; }

    // Cap on the magnitude of the integral term (anti-windup). 0 disables the
    // clamp. Units match the controller output.
    void setIntegralLimit(float iLimit) { iLimit_ = iLimit; }

    // Clear integral accumulator and derivative history. Call on (re)arm.
    void reset() {
        integral_ = 0.0f;
        prevError_ = 0.0f;
        havePrev_ = false;
    }

    // Advance the controller one step.
    //   error = setpoint - measurement
    //   dt    = seconds since the previous update (> 0)
    // Returns the control output (e.g. a desired torque about one axis).
    float update(float error, float dt);

private:
    PidGains gains_{};
    float    iLimit_   = 0.0f;   // anti-windup clamp on the integral term
    float    integral_ = 0.0f;
    float    prevError_ = 0.0f;
    bool     havePrev_  = false; // suppress the derivative spike on the first step
};
