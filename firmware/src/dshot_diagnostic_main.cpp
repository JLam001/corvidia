// First hardware-validation stage: ESC battery disconnected. Never sends throttle.
#include <Arduino.h>
#include "bench_protocol.h"
#include "dshot_output.h"
#include "rate_timer.h"

namespace {
constexpr uint32_t MODE = 0x44533330; // DS30; distinct from PWM bench and PWMD
RateTimer telemetry(5);
mavlink_message_t rx{};
mavlink_status_t rxStatus{};
unsigned field = 0;
void send(mavlink_message_t& m) {
    uint8_t bytes[MAVLINK_MAX_PACKET_LEN];
    const auto n = mavlink_msg_to_send_buffer(bytes, &m);
    if (Serial.availableForWrite() >= n) Serial.write(bytes, n);
}
void named(const char* label, int32_t v) {
    char name[10] = {};
    snprintf(name, sizeof(name), "%s", label);
    mavlink_message_t m;
    mavlink_msg_named_value_int_pack(1, 1, &m, millis(), name, v);
    send(m);
}
void command() {
    if (!BenchProtocol::fromOperator(rx) || rx.msgid != MAVLINK_MSG_ID_COMMAND_LONG) return;
    mavlink_command_long_t c;
    mavlink_msg_command_long_decode(&rx, &c);
    if (!BenchProtocol::addressed(c)) return;
    // A stop request leaves the image sending only zero frames (or no signals).
    // It cannot enable outputs, arm, run a motor or write an ESC setting.
    const bool stop = BenchProtocol::finiteParams(c) && c.param1 == 0 &&
        ((c.command == BenchProtocol::ENABLE_COMMAND && BenchProtocol::enableShape(c)) ||
         c.command == MAV_CMD_COMPONENT_ARM_DISARM);
    mavlink_message_t m;
    mavlink_msg_command_ack_pack(1, 1, &m, c.command,
        stop ? MAV_RESULT_ACCEPTED : MAV_RESULT_UNSUPPORTED,
        0, 0, BenchProtocol::OPERATOR_SYSTEM, BenchProtocol::OPERATOR_COMPONENT);
    send(m);
}
}
void setup() {
    Serial.begin(115200);
    DshotOutput::begin();
}
void loop() {
    DshotOutput::service(micros());
    for (unsigned budget = 0; budget < 128 && Serial.available(); ++budget) {
        if (mavlink_parse_char(MAVLINK_COMM_0, Serial.read(), &rx, &rxStatus)) command();
    }
    if (telemetry.due(millis())) {
        // Spread packets across ticks to respect CDC's small TX buffer.
        switch (field) {
            case 0: {
                mavlink_message_t m;
                mavlink_msg_heartbeat_pack(1, 1, &m, MAV_TYPE_QUADROTOR,
                    MAV_AUTOPILOT_INVALID, 0, MODE,
                    DshotOutput::faulted() ? MAV_STATE_CRITICAL :
                    DshotOutput::ready() ? MAV_STATE_STANDBY : MAV_STATE_UNINIT);
                send(m); break;
            }
            case 1: named("DS_FRAMES", DshotOutput::frames()); break;
            case 2: named("DS_FAULT", DshotOutput::faulted()); break;
            case 3: named("DS_OUTPUT", DshotOutput::ready()); break;
        }
        field = (field + 1) % 4;
    }
}
