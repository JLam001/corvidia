#pragma once
#include <stdint.h>

// Tracks when the peer (the Jetson) was last heard and reports liveness.
// Arduino-free and header-only so it unit-tests on a host (same pattern as
// rate_timer.h). Uses rollover-safe unsigned subtraction so it keeps working
// across the ~49.7-day millis() wrap.
struct LinkWatchdog {
    uint32_t lastSeenMs = 0;
    bool     everSeen   = false;

    void heard(uint32_t now) { lastSeenMs = now; everSeen = true; }

    bool alive(uint32_t now, uint32_t timeoutMs) const {
        return everSeen && (uint32_t)(now - lastSeenMs) <= timeoutMs;
    }
};
