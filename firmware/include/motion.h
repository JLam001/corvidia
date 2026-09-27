#pragma once
#include <cmath>
#include <cstdint>

// Body axes: x forward, y right, z down. Positive roll lowers the right side;
// positive pitch raises the nose; positive yaw turns right viewed from above.
// These are attitude requests, NOT position/velocity/altitude commands.
enum class MotionAction : uint8_t {
    LEVEL, TILT_FORWARD, TILT_BACKWARD, TILT_LEFT, TILT_RIGHT, YAW_LEFT, YAW_RIGHT
};

struct MotionRequest {
    MotionAction action = MotionAction::LEVEL;
    float amount = 0;                 // degrees for tilt, deg/s for yaw
    uint32_t durationMs = 0;          // absolute action lifetime at receiver
    uint32_t sequence = 0;
};

struct AttitudeDemand {
    float rollDeg = 0;
    float pitchDeg = 0;
    float yawRateDps = 0;
};

// One human-enabled session. Do not expose enable()/disable() to an LLM.
// Expiration or stale supervisor keepalive revokes the session; a reconnect
// never resumes an old action. LEVEL is not hover, braking, or motor disarm.
class MotionSupervisor {
public:
    static constexpr float MAX_TILT_DEG = 10;
    static constexpr float MAX_YAW_DPS = 30;
    static constexpr uint32_t MAX_DURATION_MS = 1000;
    static constexpr uint32_t KEEPALIVE_MS = 250;

    void enable(uint32_t now) {
        enabled_ = true;
        active_ = false;
        haveSequence_ = false;
        lastKeepalive_ = now;
        demand_ = {};
    }
    void disable() { enabled_ = active_ = false; demand_ = {}; }
    void keepalive(uint32_t now) {
        update(now);
        if (enabled_) lastKeepalive_ = now;
    }
    bool submit(uint32_t now, const MotionRequest& r) {
        update(now);
        if (!enabled_ || !std::isfinite(r.amount) || r.amount < 0 ||
            r.durationMs == 0 || r.durationMs > MAX_DURATION_MS ||
            (haveSequence_ && !newer(r.sequence, lastSequence_))) return false;
        AttitudeDemand next{};
        switch (r.action) {
        case MotionAction::LEVEL:
            if (r.amount != 0) return false;
            break;
        case MotionAction::TILT_FORWARD:  next.pitchDeg = -r.amount; break;
        case MotionAction::TILT_BACKWARD: next.pitchDeg = r.amount; break;
        case MotionAction::TILT_LEFT:     next.rollDeg = -r.amount; break;
        case MotionAction::TILT_RIGHT:    next.rollDeg = r.amount; break;
        case MotionAction::YAW_LEFT:      next.yawRateDps = -r.amount; break;
        case MotionAction::YAW_RIGHT:     next.yawRateDps = r.amount; break;
        default: return false;
        }
        if (std::fabs(next.rollDeg) > MAX_TILT_DEG ||
            std::fabs(next.pitchDeg) > MAX_TILT_DEG ||
            std::fabs(next.yawRateDps) > MAX_YAW_DPS) return false;
        demand_ = next;
        start_ = now;
        duration_ = r.durationMs;
        lastSequence_ = r.sequence;
        haveSequence_ = active_ = true;
        return true;
    }
    void update(uint32_t now) {
        if (enabled_ && (uint32_t)(now - lastKeepalive_) >= KEEPALIVE_MS) disable();
        if (active_ && (uint32_t)(now - start_) >= duration_) {
            active_ = false;
            demand_ = {};
        }
    }
    bool enabled() const { return enabled_; }
    bool active() const { return active_; }
    AttitudeDemand demand() const { return demand_; }

private:
    static bool newer(uint32_t a, uint32_t b) {
        const uint32_t d = a - b;
        return d != 0 && d < 0x80000000u;
    }
    bool enabled_ = false, active_ = false, haveSequence_ = false;
    uint32_t lastKeepalive_ = 0, lastSequence_ = 0, start_ = 0, duration_ = 0;
    AttitudeDemand demand_{};
};
