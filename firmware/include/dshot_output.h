#pragma once
#include <cstdint>
#include "dshot_test.h"

#ifndef CORVIDIA_DSHOT_SIGNALS
#define CORVIDIA_DSHOT_SIGNALS 0
#endif
#ifndef CORVIDIA_DSHOT_BOUNDED
#define CORVIDIA_DSHOT_BOUNDED 0
#endif
#ifndef CORVIDIA_DSHOT_MOTOR_TESTS
#define CORVIDIA_DSHOT_MOTOR_TESTS 0
#endif

// Zero-only diagnostic unless CORVIDIA_DSHOT_BOUNDED is selected. Motor tests
// additionally require CORVIDIA_DSHOT_MOTOR_TESTS. Owns TIM3/TIM4, DMA1 streams
// 2/6, and (bounded image only) TIM5. Do not link with esc_pwm.cpp.
namespace DshotOutput {
bool begin();
void service(uint32_t nowUs);
void disable(); // line low / no frames; ESC signal-loss behavior is unverified
bool ready();
bool faulted();
uint32_t frames(); // completed frame pairs; not an ESC acknowledgement
#if CORVIDIA_DSHOT_BOUNDED
bool motorTestsAllowed();
// The selected firmware profile applies its mandatory startup ramp. Callers
// cannot opt out, change its duration, or extend the fixed run deadline.
bool start(uint8_t motor, uint16_t value, uint32_t durationMs,
           uint32_t attitudeAt, uint32_t gyroAt, uint32_t heartbeatAt,
           const DshotLimits& limits = {});
void refresh(uint32_t attitudeAt, uint32_t gyroAt, uint32_t heartbeatAt);
// refresh updates health timestamps only; it cannot extend test duration.
void stop();    // next frame is zero on every channel
bool expired();
uint16_t commanded(uint8_t motor); // requested input, never measured RPM
#endif
}
