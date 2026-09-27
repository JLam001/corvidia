#include "link.h"

static const uint8_t JETSON_COMPONENT = MAV_COMP_ID_ONBOARD_COMPUTER; // 191
static const float   DEG2RAD = 0.017453292519943295f;

void Link::begin(Stream& io, uint8_t sysid, uint8_t compid) {
    io_ = &io; sysid_ = sysid; compid_ = compid;
}

void Link::send(mavlink_message_t& msg) {
    uint8_t buf[MAVLINK_MAX_PACKET_LEN];
    uint16_t len = mavlink_msg_to_send_buffer(buf, &msg);
    // Non-blocking: only write if the USB TX buffer has room, so a stalled
    // host never blocks the control loop. Drop the message otherwise.
    if (io_ && io_->availableForWrite() >= (int)len) io_->write(buf, len);
}

void Link::sendHeartbeat(uint8_t system_status) {
    mavlink_message_t m;
    mavlink_msg_heartbeat_pack(sysid_, compid_, &m, MAV_TYPE_QUADROTOR,
        MAV_AUTOPILOT_GENERIC, 0, 0, system_status);
    send(m);
}

void Link::sendAttitude(uint32_t now_ms, const Attitude& a, const Rates& r) {
    mavlink_message_t m;
    mavlink_msg_attitude_pack(sysid_, compid_, &m, now_ms,
        a.roll_deg * DEG2RAD, a.pitch_deg * DEG2RAD, a.yaw_deg * DEG2RAD,
        r.roll_dps * DEG2RAD, r.pitch_dps * DEG2RAD, r.yaw_dps * DEG2RAD);
    send(m);
}

void Link::sendAttitudeTarget(uint32_t now_ms, const Setpoint& sp) {
    // Echo the active setpoint the arbiter chose, so the Jetson can see what the
    // STM32 is actually applying (NORMAL command vs FAILSAFE level+descent).
    const float q[4] = {1.0f, 0.0f, 0.0f, 0.0f};   // identity; axes carried as body rates
    mavlink_message_t m;
    mavlink_msg_attitude_target_pack(sysid_, compid_, &m, now_ms,
        /*type_mask=*/0, q, sp.roll, sp.pitch, sp.yaw, sp.thrust);
    send(m);
}

void Link::sendStatusText(uint8_t severity, const char* text) {
    mavlink_message_t m;
    mavlink_msg_statustext_pack(sysid_, compid_, &m, severity, text, 0, 0);
    send(m);
}

void Link::poll(uint32_t now_ms) {
    if (!io_) return;
    while (io_->available() > 0) {
        uint8_t c = (uint8_t)io_->read();
        if (mavlink_parse_char(MAVLINK_COMM_0, c, &rxMsg_, &rxStatus_)) {
            if (rxMsg_.compid != JETSON_COMPONENT) continue;
            if (rxMsg_.msgid == MAVLINK_MSG_ID_HEARTBEAT) {
                wd_.heard(now_ms);
            } else if (rxMsg_.msgid == MAVLINK_MSG_ID_SET_ATTITUDE_TARGET) {
                lastCommand_   = decodeSetpoint(rxMsg_);
                lastCommandMs_ = now_ms;
                newCommand_    = true;
            }
        }
    }
}
