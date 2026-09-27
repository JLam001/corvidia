#pragma once
#include <cstdint>
#include <cmath>

#ifndef CORVIDIA_DSHOT_RAMP_MS
#define CORVIDIA_DSHOT_RAMP_MS 0
#endif
static_assert(CORVIDIA_DSHOT_RAMP_MS == 0 || CORVIDIA_DSHOT_RAMP_MS == 5000,
              "Only the legacy immediate profile and the fixed five-second ramp are supported");

// Operator-selected envelope for ONE test. Legacy/default enable stays at
// the physically exercised 5%/500 ms. The larger range is not hardware validation.
struct DshotLimits {
    static constexpr uint32_t MAX_DURATION_MS = 60000;
    float percent = 5;
    uint32_t durationMs = 500;
    bool valid() const {
        return std::isfinite(percent) && percent > 0 && percent <= 100 &&
            durationMs >= 20 && durationMs <= MAX_DURATION_MS;
    }
    uint16_t throttle() const {
        return valid() ? uint16_t(48 + std::floor(1999 * percent / 100)) : 0;
    }
};

// Operator bench commands, never flight control. No RPM estimate is implied.
class DshotTest {
public:
    static constexpr uint8_t ALL_MOTORS = 5; // internal selector; separate MAVLink command
    static constexpr uint32_t HEARTBEAT_MS = 500, ENABLE_MS = 3000;
    static constexpr uint32_t MAX_RUN_MS = DshotLimits::MAX_DURATION_MS, MAIN_MS = 50, IMU_MS = 100;
    static constexpr uint16_t MAX_THROTTLE = 2047;
    static constexpr uint32_t MAX_TOKEN = 16777215; // exactly representable float
    void heartbeat(uint32_t now) { update(now, true); seen_ = true; heartbeatAt_ = now; }
    bool enable(uint32_t now, uint32_t token, bool ready, const DshotLimits& limits = {}) {
        update(now, ready);
        if (!ready || !limits.valid() || enabled_ || !fresh(now) || token != nextToken_ ||
            nextToken_ > MAX_TOKEN) return false;
        limits_ = limits; token_ = nextToken_++; enabled_ = true; enabledAt_ = now;
        return true;
    }
    bool start(uint32_t now, uint32_t token, uint8_t motor,
               uint16_t throttle, uint32_t duration) {
        update(now, true);
        if (!enabled_ || active_ || token != token_ || motor < 1 || motor > ALL_MOTORS ||
            throttle < 48 || throttle > limits_.throttle() || duration < 20 ||
            duration > limits_.durationMs) return false;
        motor_ = motor; throttle_ = throttle; startedAt_ = now;
        duration_ = duration; active_ = true; return true;
    }
    void stop() { enabled_ = active_ = false; limits_ = {}; }
    void update(uint32_t now, bool healthy) {
        if (enabled_ && (!healthy || !fresh(now) ||
            (!active_ && uint32_t(now - enabledAt_) >= ENABLE_MS))) stop();
        if (active_ && uint32_t(now - startedAt_) >= duration_) stop();
    }
    bool enabled() const { return enabled_; }
    bool active() const { return active_; }
    uint32_t nextToken() const { return nextToken_; }
    uint32_t heartbeatAt() const { return heartbeatAt_; }
    DshotLimits limits() const { return limits_; }
    uint8_t motor() const { return active_ ? motor_ : 0; }
    uint16_t throttle() const { return active_ ? throttle_ : 0; }
private:
    DshotLimits limits_{};
    bool fresh(uint32_t now) const { return seen_ && uint32_t(now-heartbeatAt_) < HEARTBEAT_MS; }
    bool seen_ = false, enabled_ = false, active_ = false;
    uint8_t motor_ = 0;
    uint16_t throttle_ = 0;
    uint32_t nextToken_ = 1, token_ = 0, heartbeatAt_ = 0, enabledAt_ = 0;
    uint32_t startedAt_ = 0, duration_ = 0;
};

// Called from a timer ISR, independent of main-loop/I2C progress. Main updates
// freshness only, never the fixed run deadline. A late refresh cannot revive it.
class DshotLease {
public:
    static constexpr uint32_t RAMP_MS = 5000;
    bool start(uint32_t now, uint8_t motor, uint16_t value, uint32_t duration,
               uint32_t attitudeAt, uint32_t gyroAt, uint32_t heartbeatAt,
               const DshotLimits& limits = {}, uint32_t rampMs = 0) {
        if (active_ || !limits.valid() || motor < 1 || motor > DshotTest::ALL_MOTORS || value < 48 ||
            value > limits.throttle() || duration < 20 ||
            duration > limits.durationMs || (rampMs != 0 && rampMs != RAMP_MS) ||
            uint32_t(now-attitudeAt) >= DshotTest::IMU_MS ||
            uint32_t(now-gyroAt) >= DshotTest::IMU_MS ||
            uint32_t(now-heartbeatAt) >= DshotTest::HEARTBEAT_MS) return false;
        active_ = true; expired_ = false; motor_ = motor; target_ = value;
        rampMs_ = rampMs; value_ = rampMs ? 0 : value;
        attitudeAt_ = attitudeAt; gyroAt_ = gyroAt; heartbeatAt_ = heartbeatAt;
        startedAt_ = refreshedAt_ = now; duration_ = duration; return true;
    }
    void refresh(uint32_t now, uint32_t attitudeAt, uint32_t gyroAt, uint32_t heartbeatAt) {
        tick(now); // a late refresh cannot revive a deadline missed by the ISR
        if (active_) {
            refreshedAt_ = now;
            attitudeAt_ = attitudeAt; gyroAt_ = gyroAt; heartbeatAt_ = heartbeatAt;
            tick(now);
        }
    }
    void tick(uint32_t now) {
        if (active_ && (uint32_t(now-startedAt_) >= duration_ ||
                       uint32_t(now-refreshedAt_) >= DshotTest::MAIN_MS ||
                       uint32_t(now-attitudeAt_) >= DshotTest::IMU_MS ||
                       uint32_t(now-gyroAt_) >= DshotTest::IMU_MS ||
                       uint32_t(now-heartbeatAt_) >= DshotTest::HEARTBEAT_MS)) {
            active_ = false; expired_ = true; value_ = 0;
        }
        if (!active_) return;
        const uint32_t elapsed = uint32_t(now - startedAt_);
        if (rampMs_ == 0 || elapsed >= rampMs_) value_ = target_;
        else if (elapsed == 0) value_ = 0;
        // Codes 1..47 are ESC configuration commands, never ramp outputs.
        // The bounded product is at most 1999 * 4999, within uint32_t.
        else value_ = uint16_t(48 + uint32_t(target_ - 48) * elapsed / rampMs_);
    }
    void stop() { active_ = false; value_ = 0; }
    bool expired() const { return expired_; }
    bool active() const { return active_; }
    uint16_t value(uint8_t motor) const {
        return active_ && motor >= 1 && motor <= 4 &&
            (motor_ == DshotTest::ALL_MOTORS || motor == motor_) ? value_ : 0;
    }
private:
    bool active_ = false, expired_ = false;
    uint8_t motor_ = 0;
    uint16_t value_ = 0, target_ = 0;
    uint32_t startedAt_ = 0, refreshedAt_ = 0, duration_ = 0, rampMs_ = 0;
    uint32_t attitudeAt_ = 0, gyroAt_ = 0, heartbeatAt_ = 0;
};
