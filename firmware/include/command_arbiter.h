#pragma once
#include <stdint.h>

// A control setpoint. Axis fields are intentionally neutral -- whether they
// mean angles or body rates is decided in Phase 2 (the controller). thrust is
// normalized 0..1. Defaults are a safe, level, zero-thrust command.
struct Setpoint {
    float roll   = 0.0f;
    float pitch  = 0.0f;
    float yaw    = 0.0f;
    float thrust = 0.0f;
};

// IDLE     : no command has ever been received (safe boot state).
// NORMAL   : link is alive and a fresh command is being followed.
// FAILSAFE : link lost or command went stale -> level + gentle descent.
enum class Mode { IDLE, NORMAL, FAILSAFE };

// Decides the active setpoint from link + command health. Arduino-free and
// header-only so it unit-tests on a host (same pattern as link_watchdog.h /
// rate_timer.h). Encodes the ARCHITECTURE.md §4 rule: on loss of a fresh
// command, go to level + gentle descent -- NEVER hold the last setpoint.
class CommandArbiter {
public:
    // descentThrust     : throttle (0..1) held during a FAILSAFE descent.
    // commandTimeoutMs  : a command older than this is considered stale.
    explicit CommandArbiter(float descentThrust = 0.30f,
                            uint32_t commandTimeoutMs = 500)
        : descentThrust_(descentThrust), commandTimeoutMs_(commandTimeoutMs) {}

    // Record a freshly received Jetson command at time `now`.
    void command(uint32_t now, const Setpoint& sp) {
        commanded_     = sp;
        lastCommandMs_ = now;
        everCommanded_ = true;
    }

    // Recompute mode + active setpoint. `linkAlive` comes from the heartbeat
    // watchdog. Returns the new mode; active() holds the setpoint to apply.
    Mode update(uint32_t now, bool linkAlive) {
        if (!everCommanded_) {
            mode_   = Mode::IDLE;
            active_ = Setpoint{};                 // level, zero thrust
        } else if (linkAlive && commandFresh(now)) {
            mode_   = Mode::NORMAL;
            active_ = commanded_;
        } else {
            mode_   = Mode::FAILSAFE;
            active_ = Setpoint{0.0f, 0.0f, 0.0f, descentThrust_};
        }
        return mode_;
    }

    Mode            mode()   const { return mode_; }
    const Setpoint& active() const { return active_; }

private:
    // Rollover-safe: unsigned subtraction keeps working past the millis() wrap.
    bool commandFresh(uint32_t now) const {
        return (uint32_t)(now - lastCommandMs_) <= commandTimeoutMs_;
    }

    float    descentThrust_;
    uint32_t commandTimeoutMs_;
    bool     everCommanded_ = false;
    Setpoint commanded_{};
    uint32_t lastCommandMs_ = 0;
    Mode     mode_          = Mode::IDLE;
    Setpoint active_{};
};
