#pragma once
#include <cmath>
#include <cstdint>

// Track attitude and gyro separately: one live report cannot hide a stale peer.
class ImuSamples {
public:
    void reset() { attitudeSeen_ = gyroSeen_ = false; }
    bool attitude(uint32_t now, uint8_t sequence, float w, float x, float y, float z) {
        const float norm = w*w + x*x + y*y + z*z;
        if (!std::isfinite(norm) || norm < 0.25f || norm > 2.25f) {
            attitudeSeen_ = false; return false;
        }
        if (attitudeSeen_ && !forward(sequence, attitudeSequence_)) return false;
        attitudeSeen_ = true; attitudeAt_ = now; attitudeSequence_ = sequence;
        return true;
    }
    bool gyro(uint32_t now, uint8_t sequence, float x, float y, float z) {
        if (!std::isfinite(x) || !std::isfinite(y) || !std::isfinite(z)) {
            gyroSeen_ = false; return false;
        }
        if (gyroSeen_ && !forward(sequence, gyroSequence_)) return false;
        gyroSeen_ = true; gyroAt_ = now; gyroSequence_ = sequence;
        return true;
    }
    bool attitudeFresh(uint32_t now, uint32_t limit = 100) const {
        return attitudeSeen_ && uint32_t(now - attitudeAt_) < limit;
    }
    bool gyroFresh(uint32_t now, uint32_t limit = 100) const {
        return gyroSeen_ && uint32_t(now - gyroAt_) < limit;
    }
    bool healthy(uint32_t now, uint32_t limit = 100) const {
        return attitudeFresh(now, limit) && gyroFresh(now, limit);
    }
    uint32_t attitudeAt() const { return attitudeAt_; }
    uint32_t gyroAt() const { return gyroAt_; }
private:
    // SH-2 reports carry an 8-bit per-sensor sequence. Allow forward gaps and
    // wrap, reject duplicate/backward/ambiguous half-range jumps. After a large
    // loss we stay unhealthy until a forward sequence arrives or sensor reset.
    // Adafruit's I2C HAL does not fill t_us, so its reconstructed timestamps
    // are not a monotonic sample clock and must not gate freshness.
    static bool forward(uint8_t next, uint8_t previous) {
        const uint8_t delta = uint8_t(next - previous);
        return delta > 0 && delta < 128;
    }
    bool attitudeSeen_ = false, gyroSeen_ = false;
    uint32_t attitudeAt_ = 0, gyroAt_ = 0;
    uint8_t attitudeSequence_ = 0, gyroSequence_ = 0;
};
