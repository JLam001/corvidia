// IMU telemetry + operator-only bounded individual/all-four DShot tests. No mixer.
#include <Arduino.h>
#include <Wire.h>
#include "dshot_output.h"
#include "dshot_protocol.h"
#include "imu.h"
#include "rate_timer.h"

namespace {
Imu imu;
DshotTest test;
bool imuOk = false;
uint32_t bootAt = 0, resets = 0;
RateTimer attitudeTimer(50), statusTimer(40);
unsigned field = 0;
mavlink_message_t rx{};
mavlink_status_t parser{};

bool send(mavlink_message_t& m) {
    uint8_t bytes[MAVLINK_MAX_PACKET_LEN];
    const auto n = mavlink_msg_to_send_buffer(bytes, &m);
    if (Serial.availableForWrite() < n) return false;
    return Serial.write(bytes, n) == n;
}
bool named(const char* label, int32_t value) {
    char name[10]{};
    snprintf(name, sizeof(name), "%s", label);
    mavlink_message_t m;
    mavlink_msg_named_value_int_pack(1, 1, &m, millis(), name, value);
    return send(m);
}
void ack(uint16_t command, uint8_t result) {
    mavlink_message_t m;
    mavlink_msg_command_ack_pack(1, 1, &m, command, result, 0, 0,
        BenchProtocol::OPERATOR_SYSTEM, BenchProtocol::OPERATOR_COMPONENT);
    send(m);
}
bool healthy() { return imuOk && imu.healthy() && !DshotOutput::faulted(); }
void stop() { test.stop(); DshotOutput::stop(); }
void supervise() {
    if (imu.resetCount() != resets) { resets = imu.resetCount(); stop(); }
    test.update(millis(), healthy());
    if (test.active() && DshotOutput::expired()) stop();
    if (!test.active()) DshotOutput::stop();
}
void handle() {
    if (!BenchProtocol::fromOperator(rx)) return;
    supervise();
    const auto now = millis();
    if (rx.msgid == MAVLINK_MSG_ID_HEARTBEAT) {
        if (mavlink_msg_heartbeat_get_type(&rx) == MAV_TYPE_GCS) test.heartbeat(now);
        return;
    }
    if (rx.msgid != MAVLINK_MSG_ID_COMMAND_LONG) return;
    mavlink_command_long_t c;
    mavlink_msg_command_long_decode(&rx, &c);
    if (!BenchProtocol::addressed(c)) return;
    if ((c.command == DshotProtocol::ENABLE || c.command == MAV_CMD_COMPONENT_ARM_DISARM)
        && DshotProtocol::stopShape(c)) {
        stop(); ack(c.command, MAV_RESULT_ACCEPTED); return;
    }
    if (c.command == DshotProtocol::ENABLE) {
        if (!DshotProtocol::enableShape(c)) { ack(c.command, MAV_RESULT_DENIED); return; }
        const bool ready = healthy() && DshotOutput::motorTestsAllowed() &&
            uint32_t(now-bootAt) >= 3000;
        const bool ok = test.enable(now, uint32_t(c.param2), ready, DshotProtocol::limits(c));
        ack(c.command, ok ? MAV_RESULT_ACCEPTED : MAV_RESULT_TEMPORARILY_REJECTED);
    } else if (c.command == DshotProtocol::TEST || c.command == DshotProtocol::TEST_ALL) {
        const bool all = c.command == DshotProtocol::TEST_ALL;
        if (!(all ? DshotProtocol::allShape(c) : DshotProtocol::testShape(c))) {
            ack(c.command, MAV_RESULT_DENIED); return;
        }
        const auto motor = all ? DshotTest::ALL_MOTORS : uint8_t(c.param1);
        const auto throttle = DshotProtocol::throttle(c.param2);
        const auto duration = uint32_t(c.param3);
        bool ok = test.start(now, uint32_t(c.param4), motor, throttle, duration);
        if (ok && !DshotOutput::start(motor, throttle, duration, imu.lastUpdateMs(),
                                     imu.gyroUpdateMs(), test.heartbeatAt(), test.limits())) { stop(); ok = false; }
        ack(c.command, ok ? MAV_RESULT_ACCEPTED : MAV_RESULT_TEMPORARILY_REJECTED);
    } else {
        // Reject arming, PWM DO_MOTOR_TEST, setpoints and ESC-setting commands.
        ack(c.command, MAV_RESULT_UNSUPPORTED);
    }
}
bool status() {
    mavlink_message_t m;
    bool sent = false;
    switch (field) {
        case 0:
            mavlink_msg_heartbeat_pack(1, 1, &m, MAV_TYPE_QUADROTOR,
                MAV_AUTOPILOT_INVALID, 0, DshotProtocol::MODE,
                !healthy() ? MAV_STATE_CRITICAL :
                test.active() ? MAV_STATE_ACTIVE : MAV_STATE_STANDBY);
            sent = send(m); break;
        case 1: sent = named("IMU_OK", imuOk && imu.healthy()); break;
        case 2: sent = named("ATT_AGE", imuOk ? int32_t(millis()-imu.lastUpdateMs()) : -1); break;
        case 3: sent = named("DS_FAULT", DshotOutput::faulted()); break;
        case 4: sent = named("DS_OUTPUT", DshotOutput::ready()); break;
        case 5: sent = named("DS_ALLOW", DshotOutput::motorTestsAllowed()); break;
        case 6: sent = named("DS_TOKEN", test.nextToken()); break;
        case 7: sent = named("DS_ACTIVE", test.active()); break;
        case 8: sent = named("DS_EXPIRE", DshotOutput::expired()); break;
        case 9: sent = named("DS_FRAMES", DshotOutput::frames()); break;
        case 10: sent = named("M1_DS", DshotOutput::commanded(1)); break;
        case 11: sent = named("M2_DS", DshotOutput::commanded(2)); break;
        case 12: sent = named("M3_DS", DshotOutput::commanded(3)); break;
        case 13: sent = named("M4_DS", DshotOutput::commanded(4)); break;
        case 14: sent = named("GYR_AGE", imuOk ? int32_t(millis()-imu.gyroUpdateMs()) : -1); break;
        case 15: sent = named("IMU_INIT", imu.initStatus()); break;
        case 16: sent = named("IMU_ADDR", imu.address()); break;
        case 17: sent = named("I2C_MASK", imu.addressMask()); break;
        case 18: sent = named("IMU_ATT", imu.attitudeReports()); break;
        case 19: sent = named("IMU_GYR", imu.gyroReports()); break;
        case 20: sent = named("IMU_REJ", imu.rejectedReports()); break;
        case 21: sent = named("DS_ALL", DshotOutput::motorTestsAllowed()); break;
        case 22: sent = named("FW_REV", DshotProtocol::REVISION); break;
        case 23: sent = named("DS_MAXP", 100); break;
        case 24: sent = named("DS_MAXMS", DshotLimits::MAX_DURATION_MS); break;
        case 25: sent = named("DS_CAP_BP", int32_t(test.limits().percent * 100)); break;
        case 26: sent = named("DS_CAP_MS", test.limits().durationMs); break;
        case 27: sent = named("FLT_READY", 0); break;
        case 28: sent = named("AXES_OK", 0); break; // fine alignment not applied
#if CORVIDIA_DSHOT_RAMP_MS
        case 29: sent = named("DS_RAMPMS", DshotProtocol::RAMP_MS); break;
#endif
    }
    if (sent) field = (field + 1) % (DshotProtocol::RAMP_MS ? 30 : 29);
    return sent;
}
}
void setup() {
    Serial.begin(115200);
    // Keep the motor pads low while the IMU starts; no DShot timer interrupt
    // runs during its initialization handshake.
    for (const uint32_t pin : {PB8, PB9, PC6, PC7}) {
        digitalWrite(pin, LOW); pinMode(pin, OUTPUT);
    }
    Wire.begin(); Wire.setClock(400000);
    imuOk = imu.begin();
    DshotOutput::begin();
    bootAt = millis();
}
void loop() {
    supervise();
    if (imuOk) imu.update();
    supervise();
    for (unsigned budget = 0; budget < 128 && Serial.available(); ++budget) {
        if (mavlink_parse_char(MAVLINK_COMM_0, Serial.read(), &rx, &parser)) handle();
    }
    supervise();
    if (test.active()) DshotOutput::refresh(imu.lastUpdateMs(), imu.gyroUpdateMs(), test.heartbeatAt());
    const auto now = millis();
    static bool statusPending = false, attitudePending = false, quaternionPending = false;
    if (statusTimer.due(now)) statusPending = true;
    if (attitudeTimer.due(now)) attitudePending = quaternionPending = true;
    // Freshness gate prevents republishing indefinitely cached samples.
    if (!imuOk || !imu.healthy()) attitudePending = quaternionPending = false;
    if (attitudePending) {
        const auto a = imu.attitude(); const auto r = imu.rates();
        mavlink_message_t m;
        mavlink_msg_attitude_pack(1, 1, &m, imu.lastUpdateMs(),
            a.roll_deg * DEG_TO_RAD, a.pitch_deg * DEG_TO_RAD, a.yaw_deg * DEG_TO_RAD,
            r.roll_dps * DEG_TO_RAD, r.pitch_dps * DEG_TO_RAD, r.yaw_dps * DEG_TO_RAD);
        if (send(m)) attitudePending = false;
    }
    if (quaternionPending) {
        float w, x, y, z; imu.quaternion(w, x, y, z);
        const auto r = imu.rates();
        const float offset[4]{}; // no sensor-to-airframe transform claimed
        mavlink_message_t m;
        mavlink_msg_attitude_quaternion_pack(1, 1, &m, imu.lastUpdateMs(), w, x, y, z,
            r.roll_dps * DEG_TO_RAD, r.pitch_dps * DEG_TO_RAD, r.yaw_dps * DEG_TO_RAD, offset);
        if (send(m)) quaternionPending = false;
    }
    // Retry a deferred status on a later loop instead of dropping every other
    // attitude packet when the two schedules coincide in the small CDC buffer.
    if (statusPending && status()) statusPending = false;
}
