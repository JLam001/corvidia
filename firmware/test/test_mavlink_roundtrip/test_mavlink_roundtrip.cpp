// ============================================================================
//  test_mavlink_roundtrip.cpp  --  Host unit tests: MAVLink pack <-> parse
//
//  Run with:   pio test -e native -f test_mavlink_roundtrip
//
//  Proves the vendored MAVLink v2 headers compile on the host and that the
//  messages we use (HEARTBEAT, ATTITUDE, SET_ATTITUDE_TARGET, ATTITUDE_TARGET)
//  survive a pack -> byte-buffer -> parse round-trip with fields intact, and
//  that decodeSetpoint() maps SET_ATTITUDE_TARGET into our neutral Setpoint.
// ============================================================================
#include <unity.h>
#include "mavlink/common/mavlink.h"
#include "setpoint_mavlink.h"

static bool feed(uint8_t chan, const uint8_t *buf, uint16_t len, mavlink_message_t *out) {
    mavlink_status_t st;
    bool got = false;
    for (uint16_t i = 0; i < len; i++)
        if (mavlink_parse_char(chan, buf[i], out, &st)) got = true;
    return got;
}

void test_heartbeat_roundtrip() {
    mavlink_message_t msg;
    uint8_t buf[MAVLINK_MAX_PACKET_LEN];
    mavlink_msg_heartbeat_pack(1, MAV_COMP_ID_AUTOPILOT1, &msg,
        MAV_TYPE_QUADROTOR, MAV_AUTOPILOT_GENERIC, 0, 0, MAV_STATE_STANDBY);
    uint16_t len = mavlink_msg_to_send_buffer(buf, &msg);

    mavlink_message_t out;
    TEST_ASSERT_TRUE(feed(MAVLINK_COMM_0, buf, len, &out));
    TEST_ASSERT_EQUAL_UINT32(MAVLINK_MSG_ID_HEARTBEAT, out.msgid);
    TEST_ASSERT_EQUAL_UINT8(MAV_TYPE_QUADROTOR, mavlink_msg_heartbeat_get_type(&out));
}

void test_attitude_roundtrip() {
    mavlink_message_t msg;
    uint8_t buf[MAVLINK_MAX_PACKET_LEN];
    mavlink_msg_attitude_pack(1, MAV_COMP_ID_AUTOPILOT1, &msg,
        12345, 0.1f, -0.2f, 1.5f, 0.01f, -0.02f, 0.03f);
    uint16_t len = mavlink_msg_to_send_buffer(buf, &msg);

    mavlink_message_t out;
    TEST_ASSERT_TRUE(feed(MAVLINK_COMM_1, buf, len, &out));
    TEST_ASSERT_EQUAL_UINT32(MAVLINK_MSG_ID_ATTITUDE, out.msgid);
    TEST_ASSERT_FLOAT_WITHIN(1e-4, 0.1f, mavlink_msg_attitude_get_roll(&out));
    TEST_ASSERT_FLOAT_WITHIN(1e-4, 1.5f, mavlink_msg_attitude_get_yaw(&out));
}

// The Jetson's down-link command. decodeSetpoint() must map the body rates +
// thrust into our neutral Setpoint (angle-vs-rate interpretation is Phase 2).
void test_set_attitude_target_decodes_to_setpoint() {
    float q[4] = {1, 0, 0, 0};
    float thrust_body[3] = {0, 0, 0};
    mavlink_message_t msg;
    uint8_t buf[MAVLINK_MAX_PACKET_LEN];
    mavlink_msg_set_attitude_target_pack(1, MAV_COMP_ID_ONBOARD_COMPUTER, &msg,
        1000, /*target_system=*/1, /*target_component=*/MAV_COMP_ID_AUTOPILOT1,
        /*type_mask=*/0, q,
        /*body_roll_rate=*/0.5f, /*body_pitch_rate=*/-0.25f, /*body_yaw_rate=*/0.1f,
        /*thrust=*/0.7f, thrust_body);
    uint16_t len = mavlink_msg_to_send_buffer(buf, &msg);

    mavlink_message_t out;
    TEST_ASSERT_TRUE(feed(MAVLINK_COMM_0, buf, len, &out));
    TEST_ASSERT_EQUAL_UINT32(MAVLINK_MSG_ID_SET_ATTITUDE_TARGET, out.msgid);

    Setpoint sp = decodeSetpoint(out);
    TEST_ASSERT_FLOAT_WITHIN(1e-4, 0.5f,  sp.roll);
    TEST_ASSERT_FLOAT_WITHIN(1e-4, -0.25f, sp.pitch);
    TEST_ASSERT_FLOAT_WITHIN(1e-4, 0.1f,  sp.yaw);
    TEST_ASSERT_FLOAT_WITHIN(1e-4, 0.7f,  sp.thrust);
}

// Availability guard for the active-setpoint echo the STM32 sends back up.
void test_attitude_target_roundtrip() {
    float q[4] = {1, 0, 0, 0};
    float thrust_body[3] = {0, 0, 0};
    mavlink_message_t msg;
    uint8_t buf[MAVLINK_MAX_PACKET_LEN];
    (void)thrust_body;
    mavlink_msg_attitude_target_pack(1, MAV_COMP_ID_AUTOPILOT1, &msg,
        1000, /*type_mask=*/0, q, 0.0f, 0.0f, 0.0f, /*thrust=*/0.3f);
    uint16_t len = mavlink_msg_to_send_buffer(buf, &msg);

    mavlink_message_t out;
    TEST_ASSERT_TRUE(feed(MAVLINK_COMM_1, buf, len, &out));
    TEST_ASSERT_EQUAL_UINT32(MAVLINK_MSG_ID_ATTITUDE_TARGET, out.msgid);
    TEST_ASSERT_FLOAT_WITHIN(1e-4, 0.3f, mavlink_msg_attitude_target_get_thrust(&out));
}

int main(int, char **) {
    UNITY_BEGIN();
    RUN_TEST(test_heartbeat_roundtrip);
    RUN_TEST(test_attitude_roundtrip);
    RUN_TEST(test_set_attitude_target_decodes_to_setpoint);
    RUN_TEST(test_attitude_target_roundtrip);
    return UNITY_END();
}
