#pragma once
#include <cmath>
#include <cstdint>
#include "motion.h"

// Software candidate only: no ESC access or implicit flight arming.
// An invalid demand requests a separately validated flight fallback. It must
// never be interpreted as an in-flight instruction to cut motor power.
struct MissionSettings {
    float nominalCollective = 0.5f; // operator setting, NOT calibrated hover thrust
    float motorCeiling = 1.0f;      // controller/mixer authority, NOT an LLM field
    uint32_t durationMs = 60000;
    bool valid() const {
        return std::isfinite(nominalCollective) && std::isfinite(motorCeiling) &&
            nominalCollective > 0 && nominalCollective < motorCeiling &&
            motorCeiling <= 1 && durationMs > 0 && durationMs <= 1800000;
    }
};

enum class MissionState { DISABLED, RUNNING, STOPPED, EXPIRED, FAULT };
enum class MissionEnd { NONE, OPERATOR_STOP, DEADLINE, SUPERVISOR_LOST,
                        STATE_UNHEALTHY, ACTION_EXPIRED };
struct MissionDemand {
    AttitudeDemand attitude{};
    float collective = 0;
    float ceiling = 0;
    bool valid = false;
};

class MissionSession {
public:
    // Called by a trusted operator/supervisor, never directly by the LLM.
    // IDs are monotonic within one receiver lifetime, with no rollover/reuse.
    bool begin(uint32_t now, uint32_t id, const MissionSettings& settings,
               bool stateHealthy) {
        if (state_ == MissionState::RUNNING || id == 0 || id <= lastId_ ||
            !settings.valid() || !stateHealthy) return false;
        settings_ = settings; id_ = lastId_ = id; startedAt_ = now;
        state_ = MissionState::RUNNING; end_ = MissionEnd::NONE;
        haveAction_ = false; motion_.enable(now);
        return true;
    }
    // A liveness update cannot extend either mission or action duration.
    bool refresh(uint32_t now, uint32_t id, bool stateHealthy) {
        update(now, stateHealthy);
        if (state_ != MissionState::RUNNING || id != id_) return false;
        motion_.keepalive(now);
        return true;
    }
    bool submit(uint32_t now, uint32_t id, const MotionRequest& request,
                bool stateHealthy) {
        update(now, stateHealthy);
        if (state_ != MissionState::RUNNING || id != id_ ||
            !motion_.submit(now, request)) return false;
        haveAction_ = true;
        return true;
    }
    void stop() { finish(MissionState::STOPPED, MissionEnd::OPERATOR_STOP); }
    void update(uint32_t now, bool stateHealthy) {
        if (state_ != MissionState::RUNNING) return;
        if (!stateHealthy) { finish(MissionState::FAULT, MissionEnd::STATE_UNHEALTHY); return; }
        if (uint32_t(now-startedAt_) >= settings_.durationMs) {
            finish(MissionState::EXPIRED, MissionEnd::DEADLINE); return;
        }
        motion_.update(now);
        if (!motion_.enabled()) {
            finish(MissionState::FAULT, MissionEnd::SUPERVISOR_LOST); return;
        }
        if (haveAction_ && !motion_.active())
            finish(MissionState::EXPIRED, MissionEnd::ACTION_EXPIRED);
    }
    // Must be evaluated on each controller tick with current state health.
    MissionDemand demand(uint32_t now, bool stateHealthy) {
        update(now, stateHealthy);
        MissionDemand result;
        if (state_ != MissionState::RUNNING || !motion_.active()) return result;
        result.attitude = motion_.demand();
        result.collective = settings_.nominalCollective;
        result.ceiling = settings_.motorCeiling;
        result.valid = true;
        return result;
    }
    MissionState state() const { return state_; }
    MissionEnd reason() const { return end_; }
    uint32_t id() const { return id_; }
private:
    void finish(MissionState state, MissionEnd reason) {
        // Preserve the initial cause of a latched stop/fault.
        if (state_ != MissionState::RUNNING) return;
        state_ = state; end_ = reason; motion_.disable();
    }
    MissionSettings settings_{};
    MotionSupervisor motion_;
    MissionState state_ = MissionState::DISABLED;
    MissionEnd end_ = MissionEnd::NONE;
    bool haveAction_ = false;
    uint32_t id_ = 0, lastId_ = 0, startedAt_ = 0;
};
