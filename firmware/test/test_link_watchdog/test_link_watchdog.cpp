// ============================================================================
//  test_link_watchdog.cpp  --  Host unit tests for LinkWatchdog
//
//  Run with:   pio test -e native -f test_link_watchdog
//
//  Verifies the liveness/timeout logic used to detect Jetson-link loss:
//  not alive until first heartbeat, alive within the timeout, expired past it,
//  and rollover-safe across the 32-bit millis() wrap.
// ============================================================================
#include <unity.h>
#include "link_watchdog.h"

void test_not_alive_before_any_heartbeat() {
    LinkWatchdog wd;
    TEST_ASSERT_FALSE(wd.alive(1000, 1500));
}

void test_alive_within_timeout_then_expires() {
    LinkWatchdog wd;
    wd.heard(1000);
    TEST_ASSERT_TRUE(wd.alive(2000, 1500));   // 1000 ms <= 1500
    TEST_ASSERT_FALSE(wd.alive(2600, 1500));  // 1600 ms > 1500
}

void test_survives_millis_overflow() {
    LinkWatchdog wd;
    wd.heard(0xFFFFFFF0UL);
    TEST_ASSERT_TRUE(wd.alive(0x00000005UL, 1500)); // wrapped delta = 21 ms
}

int main(int, char **) {
    UNITY_BEGIN();
    RUN_TEST(test_not_alive_before_any_heartbeat);
    RUN_TEST(test_alive_within_timeout_then_expires);
    RUN_TEST(test_survives_millis_overflow);
    return UNITY_END();
}
