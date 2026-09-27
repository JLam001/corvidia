#include <unity.h>
#include <limits>
#include "dshot_protocol.h"
#include "imu_samples.h"

void test_explicit_enable_health_and_single_use_tokens() {
    DshotTest t;
    TEST_ASSERT_FALSE(t.enable(0, 1, true));
    t.heartbeat(0);
    TEST_ASSERT_FALSE(t.enable(0, 1, false));
    TEST_ASSERT_FALSE(t.enable(0, 2, true));
    TEST_ASSERT_TRUE(t.enable(0, 1, true));
    TEST_ASSERT_FALSE(t.start(0, 2, 1, 60, 500));
    TEST_ASSERT_TRUE(t.start(0, 1, 1, 60, 500));
    TEST_ASSERT_FALSE(t.start(1, 1, 2, 60, 500));
    t.stop();
    TEST_ASSERT_FALSE(t.enable(1, 1, true));
    TEST_ASSERT_FALSE(t.start(1, 1, 1, 60, 500));
    TEST_ASSERT_TRUE(t.enable(1, 2, true));
    TEST_ASSERT_FALSE(t.start(1, 1, 1, 60, 500));
}
void test_run_deadline_not_extended_by_heartbeat_or_retry() {
    DshotTest t; t.heartbeat(0); t.enable(0, 1, true); t.start(0, 1, 4, 147, 500);
    for (unsigned now = 100; now < 500; now += 100) {
        t.heartbeat(now);
        TEST_ASSERT_FALSE(t.start(now, 1, 4, 147, 500));
    }
    t.update(499, true); TEST_ASSERT_TRUE(t.active());
    t.heartbeat(500); TEST_ASSERT_FALSE(t.active()); TEST_ASSERT_FALSE(t.enabled());
}
void test_heartbeat_loss_and_sensor_failure_require_new_enable() {
    DshotTest t; t.heartbeat(0); t.enable(0, 1, true);
    t.start(490, 1, 1, 60, 500);
    t.heartbeat(500); TEST_ASSERT_FALSE(t.enabled());
    TEST_ASSERT_FALSE(t.start(501, 1, 1, 60, 500));
    TEST_ASSERT_TRUE(t.enable(501, 2, true));
    t.start(501, 2, 1, 60, 500); t.update(502, false);
    t.update(503, true); TEST_ASSERT_FALSE(t.enabled());
}
void test_enable_expires_with_continuous_heartbeats() {
    DshotTest t; t.heartbeat(0); t.enable(0, 1, true);
    for (unsigned n = 100; n <= 3000; n += 100) t.heartbeat(n);
    TEST_ASSERT_FALSE(t.enabled());
}
void test_bounds_no_throttle_settings_commands() {
    DshotTest t; t.heartbeat(0); t.enable(0, 1, true);
    TEST_ASSERT_FALSE(t.start(0, 1, 0, 48, 20));
    TEST_ASSERT_FALSE(t.start(0, 1, 6, 48, 20));
    TEST_ASSERT_FALSE(t.start(0, 1, 1, 47, 20));
    TEST_ASSERT_FALSE(t.start(0, 1, 1, 148, 20));
    TEST_ASSERT_FALSE(t.start(0, 1, 1, 48, 19));
    TEST_ASSERT_FALSE(t.start(0, 1, 1, 48, 501));
    TEST_ASSERT_TRUE(t.start(0, 1, 1, 48, 20));
    TEST_ASSERT_EQUAL(48, t.throttle());
}
void test_timer_guard_stops_if_main_stalls_and_late_refresh_cannot_revive() {
    DshotLease g;
    TEST_ASSERT_TRUE(g.start(0, 3, 147, 500, 0, 0, 0));
    TEST_ASSERT_EQUAL(0, g.value(1)); TEST_ASSERT_EQUAL(147, g.value(3));
    g.tick(49); TEST_ASSERT_TRUE(g.active());
    g.tick(50); TEST_ASSERT_FALSE(g.active()); TEST_ASSERT_TRUE(g.expired());
    g.refresh(51, 51, 51, 51); TEST_ASSERT_EQUAL(0, g.value(3));
}
void test_timer_guard_run_deadline_and_rollover() {
    DshotLease g; const uint32_t start = 0xffffff00u;
    TEST_ASSERT_TRUE(g.start(start, 4, 60, 500, start, start, start));
    for (unsigned n=10; n<500; n+=10) g.refresh(start+n, start+n, start+n, start+n);
    g.tick(start+499); TEST_ASSERT_EQUAL(60, g.value(4));
    g.tick(start+500); TEST_ASSERT_EQUAL(0, g.value(4));
    g.refresh(start+501, start+501, start+501, start+501); TEST_ASSERT_FALSE(g.active());
    TEST_ASSERT_TRUE(g.start(start+502, 1, 48, 20, start+502, start+502, start+502));
    g.stop(); TEST_ASSERT_EQUAL(0, g.value(1));
}
void test_timer_independently_checks_sensor_and_heartbeat_ages() {
    DshotLease g;
    TEST_ASSERT_FALSE(g.start(100, 1, 60, 500, 0, 100, 100));
    TEST_ASSERT_FALSE(g.start(100, 1, 60, 500, 100, 0, 100));
    TEST_ASSERT_FALSE(g.start(500, 1, 60, 500, 500, 500, 0));
    TEST_ASSERT_TRUE(g.start(90, 1, 60, 500, 0, 90, 90));
    g.refresh(99, 0, 99, 99); g.tick(100);
    TEST_ASSERT_FALSE(g.active()); TEST_ASSERT_TRUE(g.expired());
    TEST_ASSERT_TRUE(g.start(490, 1, 60, 500, 490, 490, 0));
    g.refresh(499, 499, 499, 0); g.tick(500);
    TEST_ASSERT_FALSE(g.active());
}
void assert_all_values(const DshotLease& g, uint16_t expected) {
    for (uint8_t motor=1; motor<=4; ++motor) TEST_ASSERT_EQUAL(expected,g.value(motor));
    TEST_ASSERT_EQUAL(0,g.value(0)); TEST_ASSERT_EQUAL(0,g.value(5));
}
void test_all_four_share_one_fixed_deadline_and_stop() {
    DshotTest t; DshotLease g;
    t.heartbeat(0); TEST_ASSERT_TRUE(t.enable(0,1,true));
    TEST_ASSERT_TRUE(t.start(0,1,DshotTest::ALL_MOTORS,67,200));
    TEST_ASSERT_TRUE(g.start(0,DshotTest::ALL_MOTORS,67,200,0,0,0));
    assert_all_values(g,67);
    TEST_ASSERT_FALSE(t.start(100,1,1,67,200));
    TEST_ASSERT_FALSE(g.start(100,DshotTest::ALL_MOTORS,67,200,100,100,100));
    for (unsigned n=20;n<200;n+=20) g.refresh(n,n,n,n);
    g.tick(200); t.update(200,true);
    assert_all_values(g,0); TEST_ASSERT_FALSE(t.enabled());
    TEST_ASSERT_TRUE(g.start(201,DshotTest::ALL_MOTORS,67,200,201,201,201));
    g.stop(); assert_all_values(g,0);
}
void test_all_four_stop_together_on_each_freshness_deadline() {
    DshotLease main, attitude, gyro, heartbeat;
    TEST_ASSERT_TRUE(main.start(0,5,67,500,0,0,0));
    main.tick(50); assert_all_values(main,0);
    main.refresh(51,51,51,51); assert_all_values(main,0);
    TEST_ASSERT_TRUE(attitude.start(90,5,67,500,0,90,90));
    attitude.tick(100); assert_all_values(attitude,0);
    TEST_ASSERT_TRUE(gyro.start(90,5,67,500,90,0,90));
    gyro.tick(100); assert_all_values(gyro,0);
    TEST_ASSERT_TRUE(heartbeat.start(490,5,67,500,490,490,0));
    heartbeat.tick(500); assert_all_values(heartbeat,0);
}
void test_all_command_is_explicit_and_preserves_bounds() {
    mavlink_command_long_t c{};
    c.command=DshotProtocol::TEST_ALL; c.param1=4; c.param2=1; c.param3=200; c.param4=1;
    TEST_ASSERT_TRUE(DshotProtocol::allShape(c));
    c.param1=0; TEST_ASSERT_FALSE(DshotProtocol::allShape(c));
    c.param1=1; TEST_ASSERT_FALSE(DshotProtocol::allShape(c));
    c.param1=5; TEST_ASSERT_FALSE(DshotProtocol::testShape(c));
    TEST_ASSERT_FALSE(DshotProtocol::allShape(c));
    c.param1=4; c.param2=100.01; TEST_ASSERT_FALSE(DshotProtocol::allShape(c));
    c.param2=5; c.param3=60001; TEST_ASSERT_FALSE(DshotProtocol::allShape(c));
    c.param3=500; c.confirmation=1; TEST_ASSERT_FALSE(DshotProtocol::allShape(c));
    c.confirmation=0; c.param4=0; TEST_ASSERT_FALSE(DshotProtocol::allShape(c));
    TEST_ASSERT_NOT_EQUAL(DshotProtocol::TEST,DshotProtocol::TEST_ALL);
}
void test_protocol_units_finite_bounds_and_replays() {
    mavlink_command_long_t c{};
    c.param1=1; c.param2=1; c.param3=500; c.param4=1;
    TEST_ASSERT_TRUE(DshotProtocol::testShape(c));
    c.param2=1050; TEST_ASSERT_FALSE(DshotProtocol::testShape(c));
    c.param2=5; TEST_ASSERT_TRUE(DshotProtocol::testShape(c));
    TEST_ASSERT_EQUAL(147, DshotProtocol::throttle(5));
    TEST_ASSERT_EQUAL(67, DshotProtocol::throttle(1));
    c.param2=std::numeric_limits<float>::quiet_NaN();
    TEST_ASSERT_FALSE(DshotProtocol::testShape(c));
    TEST_ASSERT_EQUAL(0, DshotProtocol::throttle(c.param2));
    c.param2=1; c.param1=1.5; TEST_ASSERT_FALSE(DshotProtocol::testShape(c));
    c.param1=1; c.param3=20.5; TEST_ASSERT_FALSE(DshotProtocol::testShape(c));
    c.param3=20; c.param4=0; TEST_ASSERT_FALSE(DshotProtocol::testShape(c));
    c.param4=1; c.confirmation=1; TEST_ASSERT_FALSE(DshotProtocol::testShape(c));
    c.confirmation=0; c.param5=1; TEST_ASSERT_FALSE(DshotProtocol::testShape(c));
    c={}; TEST_ASSERT_TRUE(DshotProtocol::stopShape(c));
    c.param1=1; c.param2=1; TEST_ASSERT_TRUE(DshotProtocol::enableShape(c));
    c.param2=16777216; TEST_ASSERT_FALSE(DshotProtocol::enableShape(c));
}
void test_mavlink_wire_keeps_units_token_and_source() {
    mavlink_message_t m{}, decoded{}; mavlink_status_t status{};
    mavlink_msg_command_long_pack(255, 190, &m, 1, 1, DshotProtocol::TEST,
                                 0, 4, 1, 200, 123, 0, 0, 0);
    uint8_t bytes[MAVLINK_MAX_PACKET_LEN];
    const auto n = mavlink_msg_to_send_buffer(bytes, &m);
    bool received=false;
    for (unsigned i=0;i<n;++i) if(mavlink_parse_char(MAVLINK_COMM_0,bytes[i],&decoded,&status)) received=true;
    TEST_ASSERT_TRUE(received); TEST_ASSERT_TRUE(BenchProtocol::fromOperator(decoded));
    mavlink_command_long_t c{}; mavlink_msg_command_long_decode(&decoded,&c);
    TEST_ASSERT_TRUE(BenchProtocol::addressed(c)); TEST_ASSERT_TRUE(DshotProtocol::testShape(c));
    TEST_ASSERT_EQUAL(DshotProtocol::TEST,c.command); TEST_ASSERT_EQUAL(123,c.param4);
    decoded.sysid=1; TEST_ASSERT_FALSE(BenchProtocol::fromOperator(decoded));
    c.target_component=0; TEST_ASSERT_FALSE(BenchProtocol::addressed(c));
}
void test_imu_requires_both_reports_and_rejects_duplicate_sequences() {
    ImuSamples s;
    TEST_ASSERT_FALSE(s.healthy(0));
    TEST_ASSERT_TRUE(s.attitude(0,1,1,0,0,0));
    TEST_ASSERT_FALSE(s.healthy(0));
    TEST_ASSERT_TRUE(s.gyro(0,1,0,0,0)); TEST_ASSERT_TRUE(s.healthy(99));
    TEST_ASSERT_FALSE(s.attitude(99,1,1,0,0,0));
    s.gyro(99,2,0,0,0); TEST_ASSERT_FALSE(s.healthy(100));
    s.attitude(100,2,1,0,0,0); TEST_ASSERT_TRUE(s.healthy(100));
    s.attitude(199,3,1,0,0,0); TEST_ASSERT_FALSE(s.healthy(199));
}
void test_imu_sequence_wrap_forward_gaps_and_backward_reports() {
    ImuSamples s;
    TEST_ASSERT_TRUE(s.attitude(0,254,1,0,0,0));
    TEST_ASSERT_TRUE(s.gyro(0,254,0,0,0));
    TEST_ASSERT_TRUE(s.attitude(1,255,1,0,0,0));
    TEST_ASSERT_TRUE(s.attitude(2,0,1,0,0,0));
    TEST_ASSERT_TRUE(s.gyro(2,1,0,0,0)); // skipped report, across wrap
    TEST_ASSERT_FALSE(s.attitude(99,255,1,0,0,0));
    TEST_ASSERT_FALSE(s.gyro(99,0,0,0,0));
    TEST_ASSERT_FALSE(s.healthy(102)); // rejected data cannot refresh ages
    TEST_ASSERT_FALSE(s.attitude(102,128,1,0,0,0)); // ambiguous half-range
    TEST_ASSERT_TRUE(s.attitude(103,1,1,0,0,0));
    TEST_ASSERT_TRUE(s.gyro(103,2,0,0,0));
    TEST_ASSERT_TRUE(s.healthy(103));
    s.reset();
    TEST_ASSERT_TRUE(s.attitude(104,0,1,0,0,0));
    TEST_ASSERT_FALSE(s.healthy(104)); // reset still requires both reports
}
void test_imu_invalid_reset_and_rollover() {
    ImuSamples s; const uint32_t now=0xfffffff0u;
    s.attitude(now,1,1,0,0,0); s.gyro(now,1,0,0,0);
    TEST_ASSERT_TRUE(s.healthy(now+99)); TEST_ASSERT_FALSE(s.healthy(now+100));
    TEST_ASSERT_FALSE(s.attitude(now+1,2,0,0,0,0)); TEST_ASSERT_FALSE(s.healthy(now+1));
    s.attitude(now+2,3,1,0,0,0);
    TEST_ASSERT_FALSE(s.gyro(now+2,2,std::numeric_limits<float>::infinity(),0,0));
    TEST_ASSERT_FALSE(s.healthy(now+2));
    s.reset(); TEST_ASSERT_FALSE(s.healthy(now+3));
    TEST_ASSERT_TRUE(s.attitude(now+3,1,1,0,0,0)); // sensor clock may restart
}
void test_extended_envelope_default_is_small_and_resets_after_stop() {
    DshotTest t; t.heartbeat(0); t.enable(0,1,true);
    TEST_ASSERT_FALSE(t.start(0,1,5,2047,60000));
    t.stop(); DshotLimits limits; limits.percent=100; limits.durationMs=60000;
    TEST_ASSERT_TRUE(t.enable(0,2,true,limits));
    TEST_ASSERT_TRUE(t.start(0,2,5,2047,60000));
    for (uint32_t n=100;n<60000;n+=100) {
        t.heartbeat(n); TEST_ASSERT_TRUE(t.active());
    }
    t.heartbeat(60000); TEST_ASSERT_FALSE(t.active());
    TEST_ASSERT_EQUAL(500,t.limits().durationMs);
    TEST_ASSERT_EQUAL(147,t.limits().throttle());
    TEST_ASSERT_FALSE(t.start(60001,2,5,2047,60000));
}
void test_extended_timer_lease_fixed_deadline_and_operator_envelope() {
    DshotLimits limits; limits.percent=100; limits.durationMs=60000;
    DshotLease g;
    TEST_ASSERT_FALSE(g.start(0,5,2047,60000,0,0,0)); // default cap
    TEST_ASSERT_TRUE(g.start(0,5,2047,60000,0,0,0,limits));
    for(uint32_t n=10;n<60000;n+=10) {
        g.refresh(n,n,n,n); TEST_ASSERT_EQUAL(2047,g.value(1));
    }
    g.tick(60000); assert_all_values(g,0);
    g.refresh(60001,60001,60001,60001); assert_all_values(g,0);
    limits.percent=10;
    TEST_ASSERT_FALSE(g.start(60002,5,2047,60000,60002,60002,60002,limits));
    limits.percent=100; limits.durationMs=60001;
    TEST_ASSERT_FALSE(g.start(60002,5,2047,60000,60002,60002,60002,limits));
}
void test_extended_protocol_limits_require_explicit_complete_enable() {
    mavlink_command_long_t c{}; c.param1=1; c.param2=1;
    TEST_ASSERT_TRUE(DshotProtocol::enableShape(c));
    TEST_ASSERT_EQUAL(147,DshotProtocol::limits(c).throttle());
    c.param3=100; TEST_ASSERT_FALSE(DshotProtocol::enableShape(c));
    c.param4=60000; TEST_ASSERT_TRUE(DshotProtocol::enableShape(c));
    TEST_ASSERT_EQUAL(2047,DshotProtocol::limits(c).throttle());
    TEST_ASSERT_EQUAL(60000,DshotProtocol::limits(c).durationMs);
    c.param4=60000.5f; TEST_ASSERT_FALSE(DshotProtocol::enableShape(c));
    c.param4=60000; c.param3=100.01f; TEST_ASSERT_FALSE(DshotProtocol::enableShape(c));
    c.param3=std::numeric_limits<float>::quiet_NaN();
    TEST_ASSERT_FALSE(DshotProtocol::enableShape(c));
}
int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_explicit_enable_health_and_single_use_tokens);
    RUN_TEST(test_run_deadline_not_extended_by_heartbeat_or_retry);
    RUN_TEST(test_heartbeat_loss_and_sensor_failure_require_new_enable);
    RUN_TEST(test_enable_expires_with_continuous_heartbeats);
    RUN_TEST(test_bounds_no_throttle_settings_commands);
    RUN_TEST(test_timer_guard_stops_if_main_stalls_and_late_refresh_cannot_revive);
    RUN_TEST(test_timer_guard_run_deadline_and_rollover);
    RUN_TEST(test_timer_independently_checks_sensor_and_heartbeat_ages);
    RUN_TEST(test_all_four_share_one_fixed_deadline_and_stop);
    RUN_TEST(test_all_four_stop_together_on_each_freshness_deadline);
    RUN_TEST(test_all_command_is_explicit_and_preserves_bounds);
    RUN_TEST(test_protocol_units_finite_bounds_and_replays);
    RUN_TEST(test_mavlink_wire_keeps_units_token_and_source);
    RUN_TEST(test_imu_requires_both_reports_and_rejects_duplicate_sequences);
    RUN_TEST(test_imu_sequence_wrap_forward_gaps_and_backward_reports);
    RUN_TEST(test_imu_invalid_reset_and_rollover);
    RUN_TEST(test_extended_envelope_default_is_small_and_resets_after_stop);
    RUN_TEST(test_extended_timer_lease_fixed_deadline_and_operator_envelope);
    RUN_TEST(test_extended_protocol_limits_require_explicit_complete_enable);
    return UNITY_END();
}
