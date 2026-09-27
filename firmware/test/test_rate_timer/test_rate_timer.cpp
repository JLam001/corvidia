// ============================================================================
//  test_rate_timer.cpp  --  Host unit tests for RateTimer
//
//  Run with:   pio test -e native
//
//  These verify the two properties that make RateTimer safe for a long-running
//  flight controller: (1) no drift when iterations run long, and (2) correct
//  behaviour across the 32-bit millis() rollover.
// ============================================================================
#include <unity.h>
#include "rate_timer.h"

// First call fires immediately (nextMs starts at 0), then holds off for one
// full period before firing again.
void test_first_call_fires_then_holds_off() {
    RateTimer t(100);          // 100 Hz => 10 ms period
    TEST_ASSERT_EQUAL_UINT32(10, t.periodMs);

    TEST_ASSERT_TRUE(t.due(0));      // first tick fires
    TEST_ASSERT_FALSE(t.due(1));     // too soon
    TEST_ASSERT_FALSE(t.due(9));     // still too soon
    TEST_ASSERT_TRUE(t.due(10));     // exactly one period later
    TEST_ASSERT_FALSE(t.due(11));
}

// Deadlines advance by whole periods, so checking slightly late does NOT push
// the schedule out -- the period stays fixed at 10 ms of wall clock, no drift.
void test_no_drift_when_checked_late() {
    RateTimer t(100);          // 10 ms period
    TEST_ASSERT_TRUE(t.due(0));

    // Check 1 ms late each time; deadline must stay on the 10/20/30 grid.
    TEST_ASSERT_TRUE(t.due(11));   // fires for the 10 ms deadline
    TEST_ASSERT_FALSE(t.due(19));
    TEST_ASSERT_TRUE(t.due(21));   // fires for the 20 ms deadline (not 21+10)
    TEST_ASSERT_FALSE(t.due(29));
    TEST_ASSERT_TRUE(t.due(31));   // fires for the 30 ms deadline
}

// After a long stall (more than a full period behind), it fires once and
// resyncs to "now" rather than firing repeatedly to catch up.
void test_resync_after_long_stall() {
    RateTimer t(100);          // 10 ms period
    TEST_ASSERT_TRUE(t.due(0));

    // Nothing ran for 100 ms. Should fire exactly once, then hold off a full
    // period from now (110), not replay the 9 missed deadlines.
    TEST_ASSERT_TRUE(t.due(100));
    TEST_ASSERT_FALSE(t.due(101));
    TEST_ASSERT_FALSE(t.due(109));
    TEST_ASSERT_TRUE(t.due(110));
}

// The comparison is rollover-safe: a timer whose deadline is just past the
// 32-bit wrap still fires correctly when `now` wraps around to a small value.
void test_survives_millis_overflow() {
    RateTimer t(100);          // 10 ms period
    const uint32_t nearMax = 0xFFFFFFFBUL;   // 5 ms before wrap

    TEST_ASSERT_TRUE(t.due(nearMax));        // fires; nextMs = nearMax + 10 = 5 (wrapped)
    TEST_ASSERT_FALSE(t.due(nearMax + 2));   // 0xFFFFFFFD, still before wrapped deadline
    TEST_ASSERT_FALSE(t.due(4));             // just after wrap, still too soon
    TEST_ASSERT_TRUE(t.due(5));              // wrapped deadline reached -> fires
    TEST_ASSERT_FALSE(t.due(6));
}

int main(int, char **) {
    UNITY_BEGIN();
    RUN_TEST(test_first_call_fires_then_holds_off);
    RUN_TEST(test_no_drift_when_checked_late);
    RUN_TEST(test_resync_after_long_stall);
    RUN_TEST(test_survives_millis_overflow);
    return UNITY_END();
}
