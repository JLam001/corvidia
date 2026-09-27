// ============================================================================
//  rate_timer.h  --  Fixed-rate, non-blocking scheduler helper
//
//  Establishes the fixed-period pattern the flight loop uses: call due(now)
//  every iteration; it returns true once per period without blocking.
//
//  Header-only and free of Arduino dependencies so it can be unit-tested on a
//  host (see test/). The control loop (Phase 2, target 500 Hz) needs finer
//  resolution than milliseconds and must use a micros()-based timebase; this
//  class is for print/status/telemetry tasks. Note that 1000/hz truncates
//  above a few hundred Hz (any rate > 1000 Hz => 0 ms => busy spin).
// ============================================================================
#pragma once

#include <stdint.h>

struct RateTimer {
    uint32_t periodMs;
    uint32_t nextMs = 0;
    bool     armed  = false;
    explicit RateTimer(uint32_t hz) : periodMs(1000UL / hz) {}

    // Returns true once per period. Advances the deadline by whole periods so
    // ticks don't drift when a loop iteration runs long, and uses rollover-safe
    // signed-difference comparison so it keeps working past the ~49.7-day
    // millis() overflow. The first call fires immediately and arms the deadline
    // relative to `now` -- we can't use nextMs == 0 as the "unarmed" sentinel
    // because a valid deadline can legitimately be 0, and a first call made when
    // now >= 2^31 would read as "not due" under the signed-difference compare.
    bool due(uint32_t now) {
        if (!armed) {
            armed  = true;
            nextMs = now + periodMs;
            return true;
        }
        if ((int32_t)(now - nextMs) < 0) {
            return false;
        }
        nextMs += periodMs;
        // If we fell more than a full period behind (long stall), resync to now
        // rather than firing repeatedly to "catch up".
        if ((int32_t)(now - nextMs) >= 0) {
            nextMs = now + periodMs;
        }
        return true;
    }
};
