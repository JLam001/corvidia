#pragma once
#include <array>
#include <cstddef>
#include <cstdint>

// Standard, non-inverted DShot. Bidirectional telemetry is not implemented.
namespace Dshot {
constexpr uint32_t BIT_RATE = 300000;
constexpr size_t BITS = 16, SLOTS = 18;
using PairBuffer = std::array<uint16_t, SLOTS * 2>;

// The throttle API cannot emit reserved commands 1..47. On error, return zero.
inline bool encodeThrottle(uint16_t throttle, uint16_t& packet) {
    packet = 0;
    if ((throttle > 0 && throttle < 48) || throttle > 2047) return false;
    const uint16_t payload = throttle << 1; // telemetry request bit is zero
    const uint16_t checksum = (payload ^ (payload >> 4) ^ (payload >> 8)) & 0xf;
    packet = (payload << 4) | checksum;
    return true;
}
struct Timing {
    uint16_t period = 0, zeroHigh = 0, oneHigh = 0;
};
inline bool timing(uint32_t timerClock, Timing& result) {
    result = {};
    if (!timerClock || timerClock % BIT_RATE) return false;
    const uint32_t period = timerClock / BIT_RATE;
    if (period < 8 || period > 65535 || period % 8) return false;
    result = {uint16_t(period), uint16_t(period * 3 / 8), uint16_t(period * 6 / 8)};
    return true;
}
// Each update event DMA-writes two adjacent CCRs. Two trailing zero slots
// leave both outputs low after the normal-mode DMA stream finishes.
inline bool makePair(uint16_t first, uint16_t second, const Timing& t,
                     PairBuffer& buffer) {
    buffer.fill(0);
    uint16_t a = 0, b = 0;
    if (!t.period || t.zeroHigh != t.period * 3 / 8 ||
        t.oneHigh != t.period * 6 / 8 || !t.zeroHigh ||
        t.oneHigh >= t.period ||
        !encodeThrottle(first, a) || !encodeThrottle(second, b)) return false;
    for (size_t bit = 0; bit < BITS; ++bit) {
        const uint16_t mask = uint16_t(1u << (15 - bit));
        buffer[bit * 2] = a & mask ? t.oneHigh : t.zeroHigh;
        buffer[bit * 2 + 1] = b & mask ? t.oneHigh : t.zeroHigh;
    }
    return true;
}
}
