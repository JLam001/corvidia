#include <unity.h>
#include <limits>
#include "mission_control.h"
#include "airframe.h"
#include "mission_protocol.h"

MotionRequest action(uint32_t seq=1, uint32_t duration=1000) {
    return {MotionAction::TILT_FORWARD, 4, duration, seq};
}
void test_settings_full_authority_with_headroom_and_no_nonfinite_values() {
    MissionSettings p;
    TEST_ASSERT_TRUE(p.valid());
    p.nominalCollective = 1; TEST_ASSERT_FALSE(p.valid());
    p.nominalCollective = .5f; p.motorCeiling = 1.01f; TEST_ASSERT_FALSE(p.valid());
    p.motorCeiling = 1; p.durationMs = 0; TEST_ASSERT_FALSE(p.valid());
    p.durationMs = 1800001; TEST_ASSERT_FALSE(p.valid());
    p.durationMs = 1800000; TEST_ASSERT_TRUE(p.valid());
    p.nominalCollective = std::numeric_limits<float>::quiet_NaN(); TEST_ASSERT_FALSE(p.valid());
}
void test_no_demand_without_operator_session_and_action() {
    MissionSession s;
    TEST_ASSERT_FALSE(s.submit(0,1,action(),true));
    TEST_ASSERT_FALSE(s.begin(0,1,{},false));
    TEST_ASSERT_TRUE(s.begin(0,1,{},true));
    TEST_ASSERT_FALSE(s.demand(0,true).valid);
    TEST_ASSERT_FALSE(s.submit(0,2,action(),true));
    TEST_ASSERT_TRUE(s.submit(0,1,action(),true));
    auto d = s.demand(0,true);
    TEST_ASSERT_TRUE(d.valid); TEST_ASSERT_EQUAL_FLOAT(.5f,d.collective);
    TEST_ASSERT_EQUAL_FLOAT(1,d.ceiling); TEST_ASSERT_EQUAL_FLOAT(-4,d.attitude.pitchDeg);
}
void test_fixed_mission_deadline_even_with_new_actions_and_keepalives() {
    MissionSession s; MissionSettings p; p.durationMs=500;
    s.begin(0,1,p,true); s.submit(0,1,action(),true);
    for (unsigned t=100;t<500;t+=100) {
        TEST_ASSERT_TRUE(s.refresh(t,1,true));
        TEST_ASSERT_TRUE(s.submit(t,1,action(t),true));
    }
    TEST_ASSERT_FALSE(s.demand(500,true).valid);
    TEST_ASSERT_TRUE(s.reason()==MissionEnd::DEADLINE);
    TEST_ASSERT_FALSE(s.refresh(501,1,true));
    TEST_ASSERT_FALSE(s.begin(501,1,p,true));
    TEST_ASSERT_TRUE(s.begin(501,2,p,true));
}
void test_heartbeat_cannot_keep_expired_llm_action_alive() {
    MissionSession s; s.begin(0,1,{},true); s.submit(0,1,action(1,200),true);
    TEST_ASSERT_TRUE(s.refresh(199,1,true));
    TEST_ASSERT_FALSE(s.refresh(200,1,true));
    TEST_ASSERT_TRUE(s.reason()==MissionEnd::ACTION_EXPIRED);
    TEST_ASSERT_FALSE(s.submit(201,1,action(2),true));
}
void test_late_or_wrong_session_keepalive_and_replay() {
    MissionSession s; s.begin(0,1,{},true); s.submit(0,1,action(),true);
    TEST_ASSERT_FALSE(s.refresh(200,2,true));
    TEST_ASSERT_FALSE(s.refresh(250,1,true));
    TEST_ASSERT_TRUE(s.reason()==MissionEnd::SUPERVISOR_LOST);
    s.begin(300,2,{},true); s.submit(300,2,action(10),true);
    TEST_ASSERT_FALSE(s.submit(301,2,action(10),true));
    TEST_ASSERT_FALSE(s.submit(301,2,action(9),true));
}
void test_stop_and_imu_fault_latch_and_wrap_is_safe() {
    MissionSession s; uint32_t t=0xfffffff0u;
    s.begin(t,1,{},true); s.submit(t,1,action(),true);
    TEST_ASSERT_TRUE(s.refresh(t+200,1,true));
    TEST_ASSERT_TRUE(s.demand(t+201,true).valid);
    TEST_ASSERT_FALSE(s.demand(t+202,false).valid);
    TEST_ASSERT_FALSE(s.demand(t+203,true).valid);
    TEST_ASSERT_TRUE(s.reason()==MissionEnd::STATE_UNHEALTHY);
    s.begin(t+204,2,{},true); s.submit(t+204,2,action(),true); s.stop();
    TEST_ASSERT_FALSE(s.demand(t+205,true).valid);
    TEST_ASSERT_FALSE(s.refresh(t+206,2,true));
    TEST_ASSERT_TRUE(s.reason()==MissionEnd::OPERATOR_STOP);
}
void test_unverified_mixer_or_gains_never_produce_a_motor_frame() {
    ControlGains gains;
    BodyState b; b.attitudeFresh=b.gyroFresh=true;
    MissionControl c(gains,plannedAirframe());
    c.session().begin(0,1,{},true); c.session().submit(0,1,action(),true);
    TEST_ASSERT_FALSE(c.update(0,b,.005f).valid);
    TEST_ASSERT_TRUE(c.session().state()==MissionState::FAULT);
}
void test_controller_adjusts_motors_and_respects_ceiling() {
    // Synthetic test fixture only; these are NOT flight gains or real yaw signs.
    ControlGains g; g.verified=true; g.angleP=2;
    for(unsigned i=0;i<3;++i) g.rateP[i]=.01f;
    QuadLayout l=plannedAirframe(); l.verified=true;
    l.yawSign[0]=1; l.yawSign[1]=-1; l.yawSign[2]=-1; l.yawSign[3]=1;
    MissionControl c(g,l); BodyState b; b.attitudeFresh=b.gyroFresh=true;
    c.session().begin(0,1,{},true); c.session().submit(0,1,action(),true);
    auto m=c.update(0,b,.005f); TEST_ASSERT_TRUE(m.valid);
    TEST_ASSERT_TRUE(m.value[l.frontLeft]<m.value[l.rearLeft]);
    for(float v:m.value) TEST_ASSERT_TRUE(v>=0 && v<=1);
    c.session().stop(); TEST_ASSERT_FALSE(c.update(1,b,.005f).valid);
}
void test_mavlink_roles_targets_and_replay() {
    MissionSession s; mavlink_message_t m;
    auto begin = [&](uint8_t source, uint8_t target, float collective) {
        mavlink_msg_command_long_pack(source,190,&m,target,1,MissionProtocol::BEGIN,0,
                                     1,collective,1,60000,0,0,0);
        return MissionProtocol::receive(s,m,0,true);
    };
    TEST_ASSERT_FALSE(begin(42,1,.5f));
    TEST_ASSERT_FALSE(begin(255,0,.5f));
    TEST_ASSERT_FALSE(begin(255,1,std::numeric_limits<float>::quiet_NaN()));
    TEST_ASSERT_TRUE(begin(255,1,.5f));
    TEST_ASSERT_FALSE(begin(255,1,.5f));
    auto move = [&](uint8_t source, float id, float seq, float duration, float code) {
        mavlink_msg_command_long_pack(source,191,&m,1,1,MissionProtocol::ACTION,0,
                                     code,4,duration,id,seq,0,0);
        return MissionProtocol::receive(s,m,1,true);
    };
    TEST_ASSERT_FALSE(move(255,1,1,500,1));
    TEST_ASSERT_FALSE(move(42,2,1,500,1));
    TEST_ASSERT_FALSE(move(42,1,1.5f,500,1));
    TEST_ASSERT_FALSE(move(42,1,1,1001,1));
    TEST_ASSERT_FALSE(move(42,1,1,500,7));
    TEST_ASSERT_TRUE(move(42,1,1,500,1));
    TEST_ASSERT_FALSE(move(42,1,1,500,1));
    TEST_ASSERT_EQUAL_FLOAT(-4,s.demand(1,true).attitude.pitchDeg);
}
int main(int,char**) {
    UNITY_BEGIN();
    RUN_TEST(test_settings_full_authority_with_headroom_and_no_nonfinite_values);
    RUN_TEST(test_no_demand_without_operator_session_and_action);
    RUN_TEST(test_fixed_mission_deadline_even_with_new_actions_and_keepalives);
    RUN_TEST(test_heartbeat_cannot_keep_expired_llm_action_alive);
    RUN_TEST(test_late_or_wrong_session_keepalive_and_replay);
    RUN_TEST(test_stop_and_imu_fault_latch_and_wrap_is_safe);
    RUN_TEST(test_unverified_mixer_or_gains_never_produce_a_motor_frame);
    RUN_TEST(test_controller_adjusts_motors_and_respects_ceiling);
    RUN_TEST(test_mavlink_roles_targets_and_replay);
    return UNITY_END();
}
