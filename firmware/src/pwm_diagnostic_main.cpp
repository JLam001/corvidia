// Diagnostic only: ESC battery MUST remain disconnected.
// Observe GPIO input levels on the four PWM output pads. This uses the MCU
// clock, not an independent oscilloscope, and does not measure ESC acceptance.
#include <Arduino.h>
#include "bench_protocol.h"
#include "esc_pwm.h"
#include "rate_timer.h"

namespace {
constexpr uint32_t MODE = 0x50574D44; // PWMD, never accepted by motor_test.py
MotorTest idle;
RateTimer heartbeat(2), reports(50);
unsigned reportIndex = 0;
struct Observation {
    uint32_t rise = 0, previousRise = 0, width = 0, period = 0, lastFall = 0;
    uint32_t samples = 0, minimum = UINT32_MAX, maximum = 0;
    bool high = false, cleanRise = false, haveRise = false;
} observed[4];
uint32_t previousPoll = 0;
void send(mavlink_message_t& m) {
    uint8_t bytes[MAVLINK_MAX_PACKET_LEN];
    const auto n = mavlink_msg_to_send_buffer(bytes, &m);
    if (Serial.availableForWrite() >= n) Serial.write(bytes, n);
}
void value(const char* name, int32_t number) {
    mavlink_message_t m;
    mavlink_msg_named_value_int_pack(1, 1, &m, millis(), name, number);
    send(m);
}
void samplePads() {
    const uint32_t now = micros();
    const bool gap = uint32_t(now - previousPoll) > 50;
    previousPoll = now;
    const uint32_t bits = ((GPIOB->IDR >> 8) & 3) | ((GPIOC->IDR >> 4) & 12);
    for (unsigned i = 0; i < 4; ++i) {
        auto& o = observed[i];
        const bool high = (bits >> i) & 1;
        if (gap) { o.cleanRise = false; o.haveRise = false; }
        if (high && !o.high) {
            if (o.haveRise) o.period = now - o.previousRise;
            o.previousRise = o.rise = now;
            o.haveRise = true;
            o.cleanRise = !gap;
        } else if (!high && o.high && o.cleanRise) {
            o.width = now - o.rise;
            o.lastFall = now;
            ++o.samples;
            if (o.width < o.minimum) o.minimum = o.width;
            if (o.width > o.maximum) o.maximum = o.width;
            o.cleanRise = false;
        }
        o.high = high;
    }
}
}
void setup() {
    Serial.begin(115200);
    EscPwm::begin(); // nominal 1000 us on every output; no spin command handler
}
void loop() {
    samplePads();
    EscPwm::write(idle);
    // Discard traffic. No incoming command can change these diagnostic outputs.
    for (unsigned n = 0; n < 32 && Serial.available(); ++n) Serial.read();
    if (heartbeat.due(millis())) {
        mavlink_message_t m;
        mavlink_msg_heartbeat_pack(1, 1, &m, MAV_TYPE_QUADROTOR,
            MAV_AUTOPILOT_INVALID, 0, MODE, MAV_STATE_STANDBY);
        send(m);
    }
    // One value per tick avoids overflowing USB CDC's small transmit buffer.
    if (reports.due(millis())) {
        const unsigned i = reportIndex / 4, field = reportIndex % 4;
        const auto& o = observed[i];
        char name[10] = {};
        const bool fresh = o.samples && uint32_t(micros() - o.lastFall) < 100000;
        switch (field) {
            case 0: snprintf(name, sizeof(name), "M%u_US", i + 1); value(name, fresh ? o.width : -1); break;
            case 1: snprintf(name, sizeof(name), "M%u_PERIOD", i + 1); value(name, fresh ? o.period : -1); break;
            case 2: snprintf(name, sizeof(name), "M%u_MIN", i + 1); value(name, o.samples ? o.minimum : -1); break;
            case 3: snprintf(name, sizeof(name), "M%u_MAX", i + 1); value(name, o.samples ? o.maximum : -1); break;
        }
        reportIndex = (reportIndex + 1) % 16;
    }
}
