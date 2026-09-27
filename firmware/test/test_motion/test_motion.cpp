#include <unity.h>
#include <limits>
#include "motion.h"

void test_disabled_rejects_and_operator_enable_allows() {
    MotionSupervisor s;
    MotionRequest r{MotionAction::TILT_FORWARD, 5, 500, 1};
    TEST_ASSERT_FALSE(s.submit(0,r)); s.enable(0);
    TEST_ASSERT_TRUE(s.submit(0,r)); TEST_ASSERT_EQUAL_FLOAT(-5,s.demand().pitchDeg);
}
void test_action_signs_and_level() {
    MotionSupervisor s; s.enable(0);
    s.submit(0,{MotionAction::TILT_BACKWARD,5,500,1}); TEST_ASSERT_EQUAL_FLOAT(5,s.demand().pitchDeg);
    s.submit(0,{MotionAction::TILT_LEFT,5,500,2}); TEST_ASSERT_EQUAL_FLOAT(-5,s.demand().rollDeg);
    s.submit(0,{MotionAction::TILT_RIGHT,5,500,3}); TEST_ASSERT_EQUAL_FLOAT(5,s.demand().rollDeg);
    s.submit(0,{MotionAction::YAW_LEFT,20,500,4}); TEST_ASSERT_EQUAL_FLOAT(-20,s.demand().yawRateDps);
    s.submit(0,{MotionAction::YAW_RIGHT,20,500,5}); TEST_ASSERT_EQUAL_FLOAT(20,s.demand().yawRateDps);
    s.submit(0,{MotionAction::LEVEL,0,500,6}); TEST_ASSERT_EQUAL_FLOAT(0,s.demand().yawRateDps);
}
void test_limits_nan_and_unknown_actions() {
    MotionSupervisor s; s.enable(0);
    TEST_ASSERT_FALSE(s.submit(0,{MotionAction::TILT_FORWARD,11,500,1}));
    TEST_ASSERT_FALSE(s.submit(0,{MotionAction::YAW_LEFT,31,500,1}));
    TEST_ASSERT_FALSE(s.submit(0,{MotionAction::LEVEL,1,500,1}));
    TEST_ASSERT_FALSE(s.submit(0,{MotionAction::LEVEL,0,1001,1}));
    TEST_ASSERT_FALSE(s.submit(0,{MotionAction::LEVEL,0,0,1}));
    TEST_ASSERT_FALSE(s.submit(0,{MotionAction::TILT_FORWARD,-1,500,1}));
    TEST_ASSERT_FALSE(s.submit(0,{MotionAction::TILT_FORWARD,std::numeric_limits<float>::quiet_NaN(),500,1}));
    TEST_ASSERT_FALSE(s.submit(0,{static_cast<MotionAction>(255),0,500,1}));
}
void test_duplicates_do_not_extend_duration() {
    MotionSupervisor s; s.enable(0);
    TEST_ASSERT_TRUE(s.submit(0,{MotionAction::TILT_FORWARD,5,200,10}));
    TEST_ASSERT_FALSE(s.submit(100,{MotionAction::TILT_FORWARD,5,200,10}));
    TEST_ASSERT_FALSE(s.submit(100,{MotionAction::TILT_FORWARD,5,200,9}));
    s.update(200); TEST_ASSERT_FALSE(s.active()); TEST_ASSERT_EQUAL_FLOAT(0,s.demand().pitchDeg);
}
void test_late_keepalive_revokes_session() {
    MotionSupervisor s; s.enable(0); s.submit(0,{MotionAction::YAW_LEFT,20,1000,1});
    s.keepalive(250); TEST_ASSERT_FALSE(s.enabled());
    TEST_ASSERT_FALSE(s.submit(251,{MotionAction::LEVEL,0,500,2}));
}
void test_rollover() {
    MotionSupervisor s; uint32_t now=0xffffff00u; s.enable(now);
    TEST_ASSERT_TRUE(s.submit(now,{MotionAction::YAW_LEFT,20,400,0xffffffffu}));
    s.keepalive(now+200);
    TEST_ASSERT_TRUE(s.submit(now+200,{MotionAction::TILT_FORWARD,2,200,0}));
    s.keepalive(now+399); s.update(now+400);
    TEST_ASSERT_FALSE(s.active()); TEST_ASSERT_EQUAL_FLOAT(0,s.demand().pitchDeg);
}
int main(int,char**) {
    UNITY_BEGIN();
    RUN_TEST(test_disabled_rejects_and_operator_enable_allows);
    RUN_TEST(test_action_signs_and_level);
    RUN_TEST(test_limits_nan_and_unknown_actions);
    RUN_TEST(test_duplicates_do_not_extend_duration);
    RUN_TEST(test_late_keepalive_revokes_session);
    RUN_TEST(test_rollover);
    return UNITY_END();
}
