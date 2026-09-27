#include <unity.h>
#include <limits>
#include "motor_test.h"
#include "bench_protocol.h"

void test_requires_enable_fresh_operator_and_ready_output() {
    MotorTest t;
    TEST_ASSERT_FALSE(t.start(0, 1, 1050, 1000));
    TEST_ASSERT_FALSE(t.enable(0, true));
    t.heartbeat(0);
    TEST_ASSERT_FALSE(t.enable(0, false));
    TEST_ASSERT_TRUE(t.enable(0, true));
    TEST_ASSERT_FALSE(t.enable(1, true));
}
void test_one_channel_and_hard_duration() {
    MotorTest t; t.heartbeat(0); t.enable(0, true);
    TEST_ASSERT_TRUE(t.start(0, 3, 1050, 1000));
    TEST_ASSERT_EQUAL(1000, t.pulse(1)); TEST_ASSERT_EQUAL(1000, t.pulse(2));
    TEST_ASSERT_EQUAL(1050, t.pulse(3)); TEST_ASSERT_EQUAL(1000, t.pulse(4));
    TEST_ASSERT_FALSE(t.start(100, 2, 1100, 2000));
    for (uint32_t now = 100; now <= 1000; now += 100) t.heartbeat(now);
    TEST_ASSERT_FALSE(t.enabled()); TEST_ASSERT_EQUAL(1000, t.pulse(3));
    TEST_ASSERT_FALSE(t.start(1001, 3, 1050, 1000));
}
void test_bounds_rejected_without_changing_state() {
    MotorTest t; t.heartbeat(0); t.enable(0, true);
    TEST_ASSERT_FALSE(t.start(0, 0, 1050, 1000));
    TEST_ASSERT_FALSE(t.start(0, 5, 1050, 1000));
    TEST_ASSERT_FALSE(t.start(0, 1, 999, 1000));
    TEST_ASSERT_FALSE(t.start(0, 1, 1101, 1000));
    TEST_ASSERT_FALSE(t.start(0, 1, 1050, 0));
    TEST_ASSERT_FALSE(t.start(0, 1, 1050, 2001));
    TEST_ASSERT_EQUAL(1000, t.pulse(1));
}
void test_link_loss_latches_stop_and_late_heartbeat_cannot_revive() {
    MotorTest t; t.heartbeat(0); t.enable(0, true); t.start(0, 1, 1050, 2000);
    t.heartbeat(500);
    TEST_ASSERT_FALSE(t.enabled()); TEST_ASSERT_EQUAL(1000, t.pulse(1));
    TEST_ASSERT_FALSE(t.start(501, 1, 1050, 2000));
}
void test_enable_lease_expires_despite_heartbeats() {
    MotorTest t; t.heartbeat(0); t.enable(0, true);
    for (uint32_t now = 100; now <= 8000; now += 100) t.heartbeat(now);
    TEST_ASSERT_FALSE(t.enabled());
}
void test_rollover_and_explicit_stop() {
    MotorTest t; const uint32_t start = 0xffffff00u;
    t.heartbeat(start); t.enable(start, true); t.start(start, 4, 1050, 300);
    t.heartbeat(start + 200); t.update(start + 299);
    TEST_ASSERT_EQUAL(1050, t.pulse(4));
    t.update(start + 300); TEST_ASSERT_EQUAL(1000, t.pulse(4));
    t.heartbeat(1000); t.enable(1000, true); t.start(1000, 4, 1050, 1000);
    t.stop(); TEST_ASSERT_FALSE(t.enabled()); TEST_ASSERT_EQUAL(1000, t.pulse(4));
}
void test_protocol_rejects_nonfinite_fractional_multi_motor_and_wrong_target() {
    mavlink_command_long_t c{};
    c.target_system=1; c.target_component=1;
    c.param1=1; c.param2=MOTOR_TEST_THROTTLE_PWM; c.param3=1050;
    c.param4=1; c.param5=1; c.param6=MOTOR_TEST_ORDER_BOARD;
    TEST_ASSERT_TRUE(BenchProtocol::motorShape(c));
    c.param1=1.5; TEST_ASSERT_FALSE(BenchProtocol::motorShape(c)); c.param1=1;
    c.param3=std::numeric_limits<float>::quiet_NaN();
    TEST_ASSERT_FALSE(BenchProtocol::motorShape(c)); c.param3=1050;
    c.param5=4; TEST_ASSERT_FALSE(BenchProtocol::motorShape(c)); c.param5=1;
    c.param2=MOTOR_TEST_THROTTLE_PERCENT; TEST_ASSERT_FALSE(BenchProtocol::motorShape(c));
    c.target_component=0; TEST_ASSERT_FALSE(BenchProtocol::addressed(c));
    mavlink_message_t msg{}; msg.sysid=1; msg.compid=MAV_COMP_ID_ONBOARD_COMPUTER;
    TEST_ASSERT_FALSE(BenchProtocol::fromOperator(msg));
    msg.sysid=255; msg.compid=MAV_COMP_ID_MISSIONPLANNER;
    TEST_ASSERT_TRUE(BenchProtocol::fromOperator(msg));
}
int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_requires_enable_fresh_operator_and_ready_output);
    RUN_TEST(test_one_channel_and_hard_duration);
    RUN_TEST(test_bounds_rejected_without_changing_state);
    RUN_TEST(test_link_loss_latches_stop_and_late_heartbeat_cannot_revive);
    RUN_TEST(test_enable_lease_expires_despite_heartbeats);
    RUN_TEST(test_rollover_and_explicit_stop);
    RUN_TEST(test_protocol_rejects_nonfinite_fractional_multi_motor_and_wrong_target);
    return UNITY_END();
}
