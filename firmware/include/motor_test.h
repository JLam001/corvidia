#pragma once
#include <cstdint>

// Props-off bench test only. One output, finite duration, fresh operator
// heartbeat, explicit enable per test. This is not an airborne failsafe.
class MotorTest {
public:
    static constexpr uint16_t STOP_US = 1000;
    static constexpr uint16_t MAX_US = 1100;
    static constexpr uint32_t MAX_RUN_MS = 2000;
    static constexpr uint32_t ENABLE_MS = 8000;
    static constexpr uint32_t HEARTBEAT_MS = 500;

    void heartbeat(uint32_t now) {
        update(now); // a late heartbeat cannot revive an expired session
        lastHeartbeat_ = now; seenHeartbeat_ = true;
    }
    bool enable(uint32_t now, bool outputReady) {
        update(now);
        if (!outputReady || enabled_ || !heartbeatFresh(now)) return false;
        enabled_ = true;
        enabledAt_ = now;
        return true;
    }
    void stop() { enabled_ = active_ = false; pulse_ = STOP_US; }
    bool start(uint32_t now, uint8_t motor, uint16_t pulseUs, uint32_t durationMs) {
        update(now);
        if (!enabled_ || active_ || motor < 1 || motor > 4 ||
            pulseUs < STOP_US || pulseUs > MAX_US ||
            durationMs == 0 || durationMs > MAX_RUN_MS) return false;
        motor_ = motor;
        pulse_ = pulseUs;
        duration_ = durationMs;
        startedAt_ = now;
        active_ = true;
        return true;
    }
    void update(uint32_t now) {
        if (enabled_ && (!heartbeatFresh(now) ||
            (uint32_t)(now - enabledAt_) >= ENABLE_MS)) stop();
        if (active_ && (uint32_t)(now - startedAt_) >= duration_) stop();
    }
    bool enabled() const { return enabled_; }
    bool active() const { return active_; }
    uint16_t pulse(uint8_t motor) const {
        return active_ && motor == motor_ ? pulse_ : STOP_US;
    }
private:
    bool heartbeatFresh(uint32_t now) const {
        return seenHeartbeat_ && (uint32_t)(now - lastHeartbeat_) < HEARTBEAT_MS;
    }
    bool seenHeartbeat_ = false, enabled_ = false, active_ = false;
    uint8_t motor_ = 0;
    uint16_t pulse_ = STOP_US;
    uint32_t lastHeartbeat_ = 0, enabledAt_ = 0, startedAt_ = 0, duration_ = 0;
};
