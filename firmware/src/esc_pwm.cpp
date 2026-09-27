#include "esc_pwm.h"
#include <HardwareTimer.h>

namespace {
HardwareTimer tim4(TIM4), tim3(TIM3), guard(TIM5);
volatile uint32_t lastRefresh = 0;
volatile bool expired = false;
bool initialized = false;
constexpr uint32_t PWM_HZ = 50; // RC input frame rate, not ESC commutation PWM

uint32_t lock() { uint32_t p = __get_PRIMASK(); __disable_irq(); return p; }
void unlock(uint32_t p) { if (!p) __enable_irq(); }

void setPulses(uint16_t a, uint16_t b, uint16_t c, uint16_t d) {
    tim4.setCaptureCompare(3, a, MICROSEC_COMPARE_FORMAT); // ESC1 PB8 / D9
    tim4.setCaptureCompare(4, b, MICROSEC_COMPARE_FORMAT); // ESC2 PB9 / D10
    tim3.setCaptureCompare(1, c, MICROSEC_COMPARE_FORMAT); // ESC3 PC6 / D6
    tim3.setCaptureCompare(2, d, MICROSEC_COMPARE_FORMAT); // ESC4 PC7 / D5
}
void stopped() { setPulses(1000, 1000, 1000, 1000); }
void deadlineISR() {
    if ((uint32_t)(millis() - lastRefresh) >= EscPwm::MAIN_DEADLINE_MS) {
        expired = true;
        stopped(); // independent of main loop; compare takes effect next frame
    }
}
}

void EscPwm::begin() {
    if (!CORVIDIA_BENCH_OUTPUTS) return;
    // The pin IDs are explicit MCU names, checked against the Feather variant.
    const uint32_t pins[] = {PB8, PB9, PC6, PC7};
    for (uint32_t pin : pins) { digitalWrite(pin, LOW); pinMode(pin, OUTPUT); }
    tim4.pause(); tim3.pause();
    tim4.setMode(3, TIMER_OUTPUT_COMPARE_PWM1, PB8);
    tim4.setMode(4, TIMER_OUTPUT_COMPARE_PWM1, PB9);
    tim3.setMode(1, TIMER_OUTPUT_COMPARE_PWM1, PC6);
    tim3.setMode(2, TIMER_OUTPUT_COMPARE_PWM1, PC7);
    tim4.setOverflow(PWM_HZ, HERTZ_FORMAT);
    tim3.setOverflow(PWM_HZ, HERTZ_FORMAT);
    stopped();
    tim4.refresh(); tim3.refresh();
    tim4.resume(); tim3.resume();
    lastRefresh = millis();
    initialized = true;
    guard.setOverflow(1000, MICROSEC_FORMAT);
    guard.attachInterrupt(deadlineISR);
    guard.setInterruptPriority(1, 0);
    guard.resume();
}
bool EscPwm::ready() { return initialized; }
bool EscPwm::timedOut() { return expired; }
void EscPwm::resetGuard() {
    if (!initialized) return;
    const auto p = lock();
    stopped(); lastRefresh = millis(); expired = false;
    unlock(p);
}
void EscPwm::write(const MotorTest& test) {
    if (!initialized) return;
    uint16_t pulses[4];
    bool valid = true;
    for (unsigned i = 0; i < 4; ++i) {
        pulses[i] = test.pulse(i + 1);
        if (pulses[i] < MotorTest::STOP_US || pulses[i] > MotorTest::MAX_US)
            valid = false;
    }
    const auto p = lock();
    if (!valid || expired) stopped();
    else setPulses(pulses[0], pulses[1], pulses[2], pulses[3]);
    lastRefresh = millis();
    unlock(p);
}
void EscPwm::stop() {
    if (!initialized) return;
    const auto p = lock(); stopped(); unlock(p);
}
