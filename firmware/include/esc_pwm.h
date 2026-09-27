#pragma once
#include <Arduino.h>
#include "motor_test.h"

// Proposed wiring after moving ESC 3: 11 -> 6 and ESC 4: 12 -> 5.
// Keep false until actual wiring AND non-reversible PWM ESC configuration
// (1000 us stopped) have been checked. This is a build-time bench interlock.
#ifndef CORVIDIA_BENCH_OUTPUTS
#define CORVIDIA_BENCH_OUTPUTS 0
#endif

namespace EscPwm {
constexpr uint32_t MAIN_DEADLINE_MS = 100;
void begin();
bool ready();
bool timedOut();
void resetGuard();                 // operator enable only; starts stopped
void write(const MotorTest& test); // main-loop refresh; range checked again
void stop();
}
