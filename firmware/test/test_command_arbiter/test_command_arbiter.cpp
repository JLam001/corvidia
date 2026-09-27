// ============================================================================
//  test_command_arbiter.cpp  --  Host unit tests for CommandArbiter
//
//  Run with:   pio test -e native -f test_command_arbiter
//
//  Verifies the link/command-health -> (Mode, active Setpoint) logic that
//  encodes the ARCHITECTURE.md §4 failsafe rule: on loss of a fresh command,
//  go to level + gentle descent -- NEVER hold the last commanded setpoint.
// ============================================================================
#include <unity.h>
#include "command_arbiter.h"

// Unity compares ints; Mode is an enum class, so cast at the call site.
#define ASSERT_MODE(expected, actual) \
    TEST_ASSERT_EQUAL_INT((int)(expected), (int)(actual))

void test_idle_before_any_command() {
    CommandArbiter arb(0.3f, 500);
    ASSERT_MODE(Mode::IDLE, arb.update(1000, /*linkAlive=*/true));
    TEST_ASSERT_EQUAL_FLOAT(0.0f, arb.active().thrust);
}

void test_normal_with_fresh_command_and_link() {
    CommandArbiter arb(0.3f, 500);
    arb.command(1000, Setpoint{0.1f, -0.2f, 0.3f, 0.6f});
    ASSERT_MODE(Mode::NORMAL, arb.update(1100, true));
    TEST_ASSERT_EQUAL_FLOAT(0.1f, arb.active().roll);
    TEST_ASSERT_EQUAL_FLOAT(0.6f, arb.active().thrust);
}

void test_failsafe_when_link_lost_never_holds_last() {
    CommandArbiter arb(0.3f, 500);
    arb.command(1000, Setpoint{0.1f, -0.2f, 0.3f, 0.9f});
    ASSERT_MODE(Mode::FAILSAFE, arb.update(1100, /*linkAlive=*/false));
    // Active must be level + descent thrust, NOT the last commanded values.
    TEST_ASSERT_EQUAL_FLOAT(0.0f, arb.active().roll);
    TEST_ASSERT_EQUAL_FLOAT(0.0f, arb.active().pitch);
    TEST_ASSERT_EQUAL_FLOAT(0.0f, arb.active().yaw);
    TEST_ASSERT_EQUAL_FLOAT(0.3f, arb.active().thrust);
}

void test_failsafe_when_command_stale_even_if_link_alive() {
    CommandArbiter arb(0.3f, 500);
    arb.command(1000, Setpoint{0, 0, 0, 0.8f});
    // 600 ms later (> 500 ms timeout) the command is stale.
    ASSERT_MODE(Mode::FAILSAFE, arb.update(1600, true));
    TEST_ASSERT_EQUAL_FLOAT(0.3f, arb.active().thrust);
}

void test_recovers_to_normal_on_fresh_command() {
    CommandArbiter arb(0.3f, 500);
    arb.command(1000, Setpoint{0, 0, 0, 0.5f});
    ASSERT_MODE(Mode::FAILSAFE, arb.update(2000, true));   // went stale
    arb.command(2000, Setpoint{0, 0, 0, 0.5f});
    ASSERT_MODE(Mode::NORMAL, arb.update(2050, true));     // fresh again
}

void test_command_freshness_survives_millis_overflow() {
    CommandArbiter arb(0.3f, 500);
    arb.command(0xFFFFFFF0UL, Setpoint{0, 0, 0, 0.5f});
    // wrapped delta = 0x05 - 0xFFFFFFF0 = 21 ms <= 500 -> still fresh
    ASSERT_MODE(Mode::NORMAL, arb.update(0x00000005UL, true));
}

int main(int, char **) {
    UNITY_BEGIN();
    RUN_TEST(test_idle_before_any_command);
    RUN_TEST(test_normal_with_fresh_command_and_link);
    RUN_TEST(test_failsafe_when_link_lost_never_holds_last);
    RUN_TEST(test_failsafe_when_command_stale_even_if_link_alive);
    RUN_TEST(test_recovers_to_normal_on_fresh_command);
    RUN_TEST(test_command_freshness_survives_millis_overflow);
    return UNITY_END();
}
