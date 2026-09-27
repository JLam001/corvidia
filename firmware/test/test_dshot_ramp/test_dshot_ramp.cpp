#include <unity.h>
#include <initializer_list>
#include "dshot_protocol.h"

namespace {
DshotLimits envelope(float percent = 25, uint32_t duration = 6000) {
    DshotLimits limits;
    limits.percent = percent; limits.durationMs = duration;
    return limits;
}
void assertAll(const DshotLease& lease, uint16_t expected) {
    for (uint8_t motor = 1; motor <= 4; ++motor)
        TEST_ASSERT_EQUAL_UINT16(expected, lease.value(motor));
    TEST_ASSERT_EQUAL_UINT16(0, lease.value(0));
    TEST_ASSERT_EQUAL_UINT16(0, lease.value(5));
}
void refreshThrough(DshotLease& lease, uint32_t start, uint32_t elapsed) {
    for (uint32_t n = 1; n <= elapsed; ++n) {
        const uint32_t now = start + n;
        lease.refresh(now, now, now, now);
    }
}
}

void test_ramp_profile_identity_and_unchanged_wire_shape() {
#if CORVIDIA_DSHOT_RAMP_MS
    TEST_ASSERT_EQUAL_HEX32(0x44534936, DshotProtocol::MODE);
    TEST_ASSERT_EQUAL_UINT32(6, DshotProtocol::REVISION);
    TEST_ASSERT_EQUAL_UINT32(5000, DshotProtocol::RAMP_MS);
#else
    TEST_ASSERT_EQUAL_HEX32(0x44534935, DshotProtocol::MODE);
    TEST_ASSERT_EQUAL_UINT32(5, DshotProtocol::REVISION);
    TEST_ASSERT_EQUAL_UINT32(0, DshotProtocol::RAMP_MS);
#endif
    mavlink_command_long_t c{};
    c.param1 = 1; c.param2 = 1; c.param3 = 25; c.param4 = 30000;
    TEST_ASSERT_TRUE(DshotProtocol::enableShape(c));
    c.param1 = 4; c.param2 = 25; c.param3 = 30000; c.param4 = 1;
    TEST_ASSERT_TRUE(DshotProtocol::allShape(c));
    c.param5 = 1; // No wire field can request a different ramp or bypass it.
    TEST_ASSERT_FALSE(DshotProtocol::allShape(c));
    c = {};
    TEST_ASSERT_TRUE(DshotProtocol::stopShape(c));
}

void test_ramp_is_monotonic_and_all_four_reach_target_at_five_seconds() {
    DshotLease lease;
    TEST_ASSERT_TRUE(lease.start(0, 5, 547, 6000, 0, 0, 0, envelope(), 5000));
    assertAll(lease, 0);
    lease.tick(0); assertAll(lease, 0);
    uint16_t previous = 0;
    for (uint32_t now = 1; now < 6000; ++now) {
        lease.refresh(now, now, now, now);
        const uint16_t value = lease.value(1);
        TEST_ASSERT_TRUE(value >= previous && value >= 48 && value <= 547);
        assertAll(lease, value);
        if (now == 1) TEST_ASSERT_EQUAL_UINT16(48, value);
        if (now == 1250) TEST_ASSERT_EQUAL_UINT16(172, value);
        if (now == 2500) TEST_ASSERT_EQUAL_UINT16(297, value);
        if (now == 3750) TEST_ASSERT_EQUAL_UINT16(422, value);
        if (now == 4999) TEST_ASSERT_EQUAL_UINT16(546, value);
        if (now >= 5000) TEST_ASSERT_EQUAL_UINT16(547, value);
        previous = value;
    }
    lease.tick(6000); assertAll(lease, 0);
}

void test_ramp_does_not_extend_short_equal_or_long_run_deadlines() {
    for (uint32_t duration : {20u, 1000u, 5000u, 6000u}) {
        DshotLease lease;
        TEST_ASSERT_TRUE(lease.start(0, 5, 547, duration, 0, 0, 0, envelope(), 5000));
        refreshThrough(lease, 0, duration - 1);
        TEST_ASSERT_TRUE(lease.active());
        if (duration <= 5000) TEST_ASSERT_TRUE(lease.value(1) < 547);
        lease.tick(duration);
        assertAll(lease, 0);
        TEST_ASSERT_TRUE(lease.expired());
        lease.refresh(duration + 1, duration + 1, duration + 1, duration + 1);
        assertAll(lease, 0);
        TEST_ASSERT_FALSE(lease.active());
    }
}

void test_stop_during_ramp_is_immediate_and_refresh_cannot_restart_it() {
    DshotLease lease;
    TEST_ASSERT_TRUE(lease.start(0, 5, 547, 6000, 0, 0, 0, envelope(), 5000));
    refreshThrough(lease, 0, 2500);
    assertAll(lease, 297);
    lease.stop(); assertAll(lease, 0);
    TEST_ASSERT_FALSE(lease.active());
    lease.refresh(2501, 2501, 2501, 2501); assertAll(lease, 0);
    lease.tick(5000); assertAll(lease, 0);
}

void test_ramp_individual_mode_leaves_other_channels_zero() {
    for (uint8_t selected = 1; selected <= 4; ++selected) {
        DshotLease lease;
        TEST_ASSERT_TRUE(lease.start(0, selected, 547, 6000, 0, 0, 0, envelope(), 5000));
        refreshThrough(lease, 0, 2500);
        for (uint8_t motor = 1; motor <= 4; ++motor)
            TEST_ASSERT_EQUAL_UINT16(motor == selected ? 297 : 0, lease.value(motor));
        lease.stop(); assertAll(lease, 0);
    }
}

void test_all_watchdogs_preempt_ramp_and_late_refresh_cannot_revive() {
    DshotLease main, attitude, gyro, heartbeat;
    TEST_ASSERT_TRUE(main.start(0, 5, 547, 6000, 0, 0, 0, envelope(), 5000));
    main.tick(49); TEST_ASSERT_TRUE(main.value(1) >= 48);
    main.tick(50); assertAll(main, 0); TEST_ASSERT_TRUE(main.expired());
    main.refresh(51, 51, 51, 51); assertAll(main, 0);

    TEST_ASSERT_TRUE(attitude.start(90, 5, 547, 6000, 0, 90, 90, envelope(), 5000));
    attitude.tick(99); TEST_ASSERT_TRUE(attitude.value(1) >= 48);
    attitude.tick(100); assertAll(attitude, 0); TEST_ASSERT_TRUE(attitude.expired());
    attitude.refresh(101, 101, 101, 101); assertAll(attitude, 0);

    TEST_ASSERT_TRUE(gyro.start(90, 5, 547, 6000, 90, 0, 90, envelope(), 5000));
    gyro.tick(99); TEST_ASSERT_TRUE(gyro.value(1) >= 48);
    gyro.tick(100); assertAll(gyro, 0); TEST_ASSERT_TRUE(gyro.expired());
    gyro.refresh(101, 101, 101, 101); assertAll(gyro, 0);

    TEST_ASSERT_TRUE(heartbeat.start(490, 5, 547, 6000, 490, 490, 0, envelope(), 5000));
    heartbeat.tick(499); TEST_ASSERT_TRUE(heartbeat.value(1) >= 48);
    heartbeat.tick(500); assertAll(heartbeat, 0); TEST_ASSERT_TRUE(heartbeat.expired());
    heartbeat.refresh(501, 501, 501, 501); assertAll(heartbeat, 0);
}

void test_delayed_timer_callback_checks_expiry_before_ramp_endpoint() {
    DshotLease lease;
    TEST_ASSERT_TRUE(lease.start(0, 5, 547, 6000, 0, 0, 0, envelope(), 5000));
    refreshThrough(lease, 0, 4999);
    assertAll(lease, 546);
    // A delayed timer service must stop for the 50 ms main freshness limit,
    // instead of briefly issuing the target at the missed ramp endpoint.
    lease.tick(5049); assertAll(lease, 0);
    TEST_ASSERT_TRUE(lease.expired());
    lease.refresh(5050, 5050, 5050, 5050); assertAll(lease, 0);
}

void test_ramp_and_duration_use_unsigned_elapsed_time_across_rollover() {
    DshotLease lease;
    const uint32_t start = 0xffffff00u;
    TEST_ASSERT_TRUE(lease.start(start, 5, 547, 6000, start, start, start, envelope(), 5000));
    for (uint32_t elapsed = 1; elapsed < 6000; ++elapsed) {
        const uint32_t now = start + elapsed;
        lease.refresh(now, now, now, now);
        TEST_ASSERT_TRUE(lease.active());
        if (elapsed == 2500) assertAll(lease, 297);
        if (elapsed == 5000) assertAll(lease, 547);
    }
    lease.tick(start + 6000); assertAll(lease, 0);
    lease.refresh(start + 6001, start + 6001, start + 6001, start + 6001);
    assertAll(lease, 0);
}

void test_replayed_start_or_enable_cannot_reset_ramp_or_deadline() {
    DshotTest command; DshotLease lease;
    command.heartbeat(0);
    TEST_ASSERT_TRUE(command.enable(0, 1, true, envelope()));
    TEST_ASSERT_TRUE(command.start(0, 1, 5, 547, 6000));
    TEST_ASSERT_TRUE(lease.start(0, 5, 547, 6000, 0, 0, 0, envelope(), 5000));
    for (uint32_t now = 1; now <= 6000; ++now) {
        command.heartbeat(now);
        lease.refresh(now, now, now, now);
        if (now == 2500) {
            TEST_ASSERT_FALSE(command.enable(now, 2, true, envelope()));
            TEST_ASSERT_FALSE(command.start(now, 1, 5, 547, 6000));
            TEST_ASSERT_FALSE(lease.start(now, 5, 547, 6000, now, now, now, envelope(), 5000));
            assertAll(lease, 297);
        }
        if (now == 5000) assertAll(lease, 547);
    }
    TEST_ASSERT_FALSE(command.active()); TEST_ASSERT_FALSE(command.enabled());
    assertAll(lease, 0);
    TEST_ASSERT_FALSE(command.start(6001, 1, 5, 547, 6000));
    TEST_ASSERT_FALSE(command.enable(6001, 1, true, envelope()));
    TEST_ASSERT_TRUE(command.enable(6001, 2, true, envelope()));
}

void test_minimum_and_maximum_targets_never_emit_special_commands() {
    for (uint16_t target : {48u, 2047u}) {
        DshotLease lease;
        TEST_ASSERT_TRUE(lease.start(0, 5, target, 6000, 0, 0, 0, envelope(100), 5000));
        assertAll(lease, 0);
        uint16_t previous = 0;
        for (uint32_t now = 1; now <= 5000; ++now) {
            lease.refresh(now, now, now, now);
            const uint16_t value = lease.value(1);
            TEST_ASSERT_TRUE(value >= 48 && value <= target && value >= previous);
            previous = value;
        }
        assertAll(lease, target);
    }
}

void test_ramp_validation_preserves_operator_envelope_and_legacy_default() {
    DshotLease lease;
    for (uint32_t invalid : {1u, 4999u, 5001u, 0xffffffffu})
        TEST_ASSERT_FALSE(lease.start(0, 5, 547, 6000, 0, 0, 0, envelope(), invalid));
    TEST_ASSERT_FALSE(lease.start(0, 5, 548, 6000, 0, 0, 0, envelope(), 5000));
    TEST_ASSERT_FALSE(lease.start(0, 5, 547, 6001, 0, 0, 0, envelope(), 5000));
    TEST_ASSERT_FALSE(lease.start(0, 5, 47, 6000, 0, 0, 0, envelope(), 5000));
    TEST_ASSERT_FALSE(lease.start(100, 5, 547, 6000, 0, 100, 100, envelope(), 5000));
    // Existing callers/builds still select zero ramp by default.
    TEST_ASSERT_TRUE(lease.start(0, 5, 147, 500, 0, 0, 0));
    assertAll(lease, 147);
    lease.stop(); assertAll(lease, 0);
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_ramp_profile_identity_and_unchanged_wire_shape);
    RUN_TEST(test_ramp_is_monotonic_and_all_four_reach_target_at_five_seconds);
    RUN_TEST(test_ramp_does_not_extend_short_equal_or_long_run_deadlines);
    RUN_TEST(test_stop_during_ramp_is_immediate_and_refresh_cannot_restart_it);
    RUN_TEST(test_ramp_individual_mode_leaves_other_channels_zero);
    RUN_TEST(test_all_watchdogs_preempt_ramp_and_late_refresh_cannot_revive);
    RUN_TEST(test_delayed_timer_callback_checks_expiry_before_ramp_endpoint);
    RUN_TEST(test_ramp_and_duration_use_unsigned_elapsed_time_across_rollover);
    RUN_TEST(test_replayed_start_or_enable_cannot_reset_ramp_or_deadline);
    RUN_TEST(test_minimum_and_maximum_targets_never_emit_special_commands);
    RUN_TEST(test_ramp_validation_preserves_operator_envelope_and_legacy_default);
    return UNITY_END();
}
