// Separate props-off motor test image. It never implements flight arming,
// SET_ATTITUDE_TARGET, autonomous motion, calibration, or a motor mixer.
#include <Arduino.h>
#include "bench_protocol.h"
#include "esc_pwm.h"
#include "rate_timer.h"

namespace {
MotorTest test;
RateTimer heartbeatTimer(5);
mavlink_message_t rx{};
mavlink_status_t status{};
uint32_t bootAt = 0;
void send(mavlink_message_t& m) {
    uint8_t data[MAVLINK_MAX_PACKET_LEN];
    const auto n = mavlink_msg_to_send_buffer(data, &m);
    if (Serial.availableForWrite() >= n) Serial.write(data, n);
}
void ack(uint16_t command, uint8_t result) {
    mavlink_message_t m;
    mavlink_msg_command_ack_pack(BenchProtocol::SYSTEM, BenchProtocol::COMPONENT,
        &m, command, result, 0, 0, BenchProtocol::OPERATOR_SYSTEM,
        BenchProtocol::OPERATOR_COMPONENT);
    send(m);
}
void handle(uint32_t now) {
    if (!BenchProtocol::fromOperator(rx)) return;
    if (rx.msgid == MAVLINK_MSG_ID_HEARTBEAT) {
        if (mavlink_msg_heartbeat_get_type(&rx) == MAV_TYPE_GCS) test.heartbeat(now);
        return;
    }
    if (rx.msgid != MAVLINK_MSG_ID_COMMAND_LONG) return;
    mavlink_command_long_t c;
    mavlink_msg_command_long_decode(&rx, &c);
    if (!BenchProtocol::addressed(c)) return;
    if (c.command == BenchProtocol::ENABLE_COMMAND) {
        if (!BenchProtocol::enableShape(c)) { ack(c.command, MAV_RESULT_DENIED); return; }
        if (c.param1 == 0) { test.stop(); EscPwm::stop(); ack(c.command, MAV_RESULT_ACCEPTED); return; }
        const bool ready = EscPwm::ready() && (uint32_t)(now - bootAt) >= 3000;
        if (test.enable(now, ready)) {
            EscPwm::resetGuard();
            ack(c.command, MAV_RESULT_ACCEPTED);
        } else ack(c.command, MAV_RESULT_TEMPORARILY_REJECTED);
    } else if (c.command == MAV_CMD_DO_MOTOR_TEST) {
        if (!BenchProtocol::motorShape(c)) { ack(c.command, MAV_RESULT_DENIED); return; }
        const bool accepted = test.start(now, (uint8_t)c.param1, (uint16_t)c.param3,
                                         (uint32_t)(c.param4 * 1000));
        ack(c.command, accepted ? MAV_RESULT_ACCEPTED : MAV_RESULT_TEMPORARILY_REJECTED);
    } else if (c.command == MAV_CMD_COMPONENT_ARM_DISARM && c.param1 == 0) {
        test.stop(); EscPwm::stop(); ack(c.command, MAV_RESULT_ACCEPTED);
    } else {
        // In particular, reject flight arming even if a client requests it.
        ack(c.command, MAV_RESULT_UNSUPPORTED);
    }
}
}

void setup() {
    Serial.begin(115200);
    bootAt = millis();
    EscPwm::begin(); // stopped pulses only, or no output with interlock disabled
}
void loop() {
    if (EscPwm::timedOut()) test.stop();
    uint32_t now = millis();
    test.update(now);
    // Bound parser work so a flood cannot starve expiry checking.
    for (unsigned budget = 0; budget < 256 && Serial.available() > 0; ++budget) {
        if (mavlink_parse_char(MAVLINK_COMM_0, Serial.read(), &rx, &status))
            handle(millis());
    }
    now = millis();
    test.update(now);
    EscPwm::write(test);
    if (heartbeatTimer.due(now)) {
        mavlink_message_t m;
        mavlink_msg_heartbeat_pack(BenchProtocol::SYSTEM, BenchProtocol::COMPONENT,
            &m, MAV_TYPE_QUADROTOR, MAV_AUTOPILOT_INVALID, 0,
            BenchProtocol::MODE_ID, EscPwm::ready() ? MAV_STATE_STANDBY : MAV_STATE_UNINIT);
        send(m);
    }
}
