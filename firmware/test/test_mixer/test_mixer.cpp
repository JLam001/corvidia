#include <unity.h>
#include <limits>
#include "airframe.h"
#include "attitude_control.h"

QuadLayout fixture() { // test convention only; not measured motor directions
    auto l=plannedAirframe(); l.verified=true;
    l.yawSign[0]=1; l.yawSign[1]=-1; l.yawSign[2]=-1; l.yawSign[3]=1;
    return l;
}
void test_unverified_or_duplicate_layout_rejected() {
    TEST_ASSERT_FALSE(mixQuadX(plannedAirframe(),0.4f,{}).valid);
    auto l=fixture(); l.frontLeft=l.frontRight;
    TEST_ASSERT_FALSE(mixQuadX(l,0.4f,{}).valid);
    l=fixture(); l.yawSign[0]=0; TEST_ASSERT_FALSE(l.valid());
}
void test_motor_map_and_torque_directions() {
    auto l=fixture();
    auto r=mixQuadX(l,0.4f,{0.1f,0,0});
    TEST_ASSERT_TRUE(r.valid);
    TEST_ASSERT_FLOAT_WITHIN(1e-6,0.5,r.value[2]); TEST_ASSERT_FLOAT_WITHIN(1e-6,0.5,r.value[3]);
    TEST_ASSERT_FLOAT_WITHIN(1e-6,0.3,r.value[0]); TEST_ASSERT_FLOAT_WITHIN(1e-6,0.3,r.value[1]);
    r=mixQuadX(l,0.4f,{0,0.1f,0});
    TEST_ASSERT_FLOAT_WITHIN(1e-6,0.5,r.value[1]); TEST_ASSERT_FLOAT_WITHIN(1e-6,0.5,r.value[3]);
    r=mixQuadX(l,0.4f,{0,0,0.1f});
    TEST_ASSERT_FLOAT_WITHIN(1e-6,0.5,r.value[0]); TEST_ASSERT_FLOAT_WITHIN(1e-6,0.5,r.value[3]);
}
void test_saturation_preserves_bounds_and_zero_collective_stops() {
    auto r=mixQuadX(fixture(),0.95f,{1,1,1});
    TEST_ASSERT_TRUE(r.valid);
    for (float x:r.value) TEST_ASSERT_TRUE(x>=0 && x<=1);
    r=mixQuadX(fixture(),0,{1,1,1});
    for (float x:r.value) TEST_ASSERT_EQUAL_FLOAT(0,x);
    TEST_ASSERT_FALSE(mixQuadX(fixture(),std::numeric_limits<float>::quiet_NaN(),{}).valid);
    TEST_ASSERT_FALSE(mixQuadX(fixture(),0.5f,{},0.2f).valid);
}
ControlGains gains() { // synthetic values for algebra tests, not flight gains
    ControlGains g; g.verified=true; g.angleP=2;
    for (unsigned i=0;i<3;++i) {g.rateP[i]=0.01f; g.rateI[i]=0.001f; g.rateD[i]=0.001f;}
    return g;
}
BodyState state() { BodyState s; s.attitudeFresh=s.gyroFresh=true; return s; }
void test_controller_requires_gains_and_both_fresh_reports() {
    AttitudeController a(ControlGains{}); TEST_ASSERT_FALSE(a.update({},state(),0.005f).valid);
    AttitudeController b(gains()); auto s=state();
    s.gyroFresh=false; TEST_ASSERT_FALSE(b.update({},s,0.005f).valid);
    s=state(); s.attitudeFresh=false; TEST_ASSERT_FALSE(b.update({},s,0.005f).valid);
    TEST_ASSERT_FALSE(b.update({},state(),0.1f).valid);
}
void test_controller_restoring_sign_reset_and_bounded_output() {
    AttitudeController a(gains()); auto s=state(); s.rollDeg=5;
    auto r=a.update({},s,0.005f); TEST_ASSERT_TRUE(r.valid); TEST_ASSERT_LESS_THAN_FLOAT(0,r.effort.roll);
    s.rollDeg=0; s.pitchDeg=-5; r=a.update({},s,0.005f);
    TEST_ASSERT_GREATER_THAN_FLOAT(0,r.effort.pitch);
    a.reset(); r=a.update({10,10,30},state(),0.005f);
    TEST_ASSERT_TRUE(std::fabs(r.effort.roll)<=0.15f && std::fabs(r.effort.yaw)<=0.15f);
    s=state(); s.rollDps=std::numeric_limits<float>::infinity();
    TEST_ASSERT_FALSE(a.update({},s,0.005f).valid);
}
void test_user_confirmed_layout_corrects_roll_pitch_and_yaw() {
    auto l=plannedAirframe(); l.verified=true; // permit software evaluation only
    TEST_ASSERT_TRUE(l.valid());
    AttitudeController c(gains()); auto s=state();
    s.rollDeg=10; // right side down: increase right 1/2, decrease left 3/4
    auto r=c.update({},s,.005f); auto m=mixQuadX(l,.5f,r.effort);
    TEST_ASSERT_TRUE(m.valid);
    TEST_ASSERT_TRUE(m.value[0]>.5f && m.value[1]>.5f);
    TEST_ASSERT_TRUE(m.value[2]<.5f && m.value[3]<.5f);
    c.reset(); s=state(); s.pitchDeg=10; // nose up: rear 1/3 increase
    r=c.update({},s,.005f); m=mixQuadX(l,.5f,r.effort);
    TEST_ASSERT_TRUE(m.value[0]>.5f && m.value[2]>.5f);
    TEST_ASSERT_TRUE(m.value[1]<.5f && m.value[3]<.5f);
    c.reset(); s=state(); s.yawDps=10; // turning right: CW rotors 1/4 increase
    r=c.update({},s,.005f); m=mixQuadX(l,.5f,r.effort);
    TEST_ASSERT_TRUE(m.value[0]>.5f && m.value[3]>.5f);
    TEST_ASSERT_TRUE(m.value[1]<.5f && m.value[2]<.5f);
}
int main(int,char**) {
    UNITY_BEGIN();
    RUN_TEST(test_unverified_or_duplicate_layout_rejected);
    RUN_TEST(test_motor_map_and_torque_directions);
    RUN_TEST(test_saturation_preserves_bounds_and_zero_collective_stops);
    RUN_TEST(test_controller_requires_gains_and_both_fresh_reports);
    RUN_TEST(test_controller_restoring_sign_reset_and_bounded_output);
    RUN_TEST(test_user_confirmed_layout_corrects_roll_pitch_and_yaw);
    return UNITY_END();
}
