#pragma once
#include <cmath>
#include <cstdint>

struct AxisEffort { float roll = 0, pitch = 0, yaw = 0; };
struct MotorFrame { float value[4] = {0, 0, 0, 0}; bool valid = false; };

// Must be measured on the assembled frame; never infer motor positions from
// where their solder pads sit on a 4-in-1 ESC. Indices are ESC outputs minus 1.
struct QuadLayout {
    uint8_t frontLeft = 0, frontRight = 0, rearLeft = 0, rearRight = 0;
    // +1: increasing this motor creates positive body yaw torque (CCW rotor
    // seen from above). -1: CW rotor. Values must alternate around the frame.
    int8_t yawSign[4] = {0, 0, 0, 0};
    bool verified = false;
    bool valid() const {
        if (!verified) return false;
        const uint8_t ids[] = {frontLeft, frontRight, rearLeft, rearRight};
        uint8_t mask = 0;
        for (uint8_t i : ids) {
            if (i > 3 || (mask & (1u << i))) return false;
            mask |= 1u << i;
            if (yawSign[i] != 1 && yawSign[i] != -1) return false;
        }
        return yawSign[frontLeft] == yawSign[rearRight] &&
               yawSign[frontRight] == yawSign[rearLeft] &&
               yawSign[frontLeft] == -yawSign[frontRight];
    }
};

// Equal-arm X quad. Inputs are normalized collective and torque corrections.
// Scale the full correction vector together on saturation to preserve its
// direction. Zero collective always means zero motor request.
inline MotorFrame mixQuadX(const QuadLayout& layout, float collective,
                          const AxisEffort& effort, float ceiling = 1.0f) {
    MotorFrame result;
    if (!layout.valid() || !std::isfinite(collective) ||
        !std::isfinite(effort.roll) || !std::isfinite(effort.pitch) ||
        !std::isfinite(effort.yaw) || !std::isfinite(ceiling) ||
        ceiling <= 0 || ceiling > 1 || collective < 0 || collective > ceiling)
        return result;
    result.valid = true;
    if (collective == 0) return result;
    float d[4]{};
    d[layout.frontLeft]  =  effort.roll + effort.pitch;
    d[layout.frontRight] = -effort.roll + effort.pitch;
    d[layout.rearLeft]   =  effort.roll - effort.pitch;
    d[layout.rearRight]  = -effort.roll - effort.pitch;
    float scale = 1;
    for (unsigned i = 0; i < 4; ++i) {
        d[i] += layout.yawSign[i] * effort.yaw;
        if (!std::isfinite(d[i])) return MotorFrame{};
        if (d[i] > 0) scale = std::fmin(scale, (ceiling - collective) / d[i]);
        if (d[i] < 0) scale = std::fmin(scale, collective / -d[i]);
    }
    for (unsigned i = 0; i < 4; ++i)
        result.value[i] = std::fmax(0.0f, std::fmin(ceiling, collective + scale * d[i]));
    return result;
}
