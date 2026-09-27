#pragma once
#include <Arduino.h>
#include "imu.h"
#include "link_watchdog.h"
#include "setpoint_mavlink.h"        // Setpoint + decodeSetpoint (pulls in mavlink.h)

// MAVLink link to the Jetson over a Stream (the USB-CDC Serial). Streams
// telemetry up (ATTITUDE + the active-setpoint echo ATTITUDE_TARGET), exchanges
// heartbeats, carries human-readable STATUSTEXT, parses the Jetson's down-link
// commands (SET_ATTITUDE_TARGET), and tracks Jetson liveness for the watchdog.
class Link {
public:
    void begin(Stream& io, uint8_t sysid = 1,
               uint8_t compid = MAV_COMP_ID_AUTOPILOT1);
    // system_status reflects the flight mode (STANDBY/ACTIVE/CRITICAL).
    void sendHeartbeat(uint8_t system_status = MAV_STATE_STANDBY);
    void sendAttitude(uint32_t now_ms, const Attitude& a, const Rates& r);
    void sendAttitudeTarget(uint32_t now_ms, const Setpoint& sp);
    void sendStatusText(uint8_t severity, const char* text);
    void poll(uint32_t now_ms);                 // non-blocking read + parse
    bool jetsonAlive(uint32_t now_ms, uint32_t timeout_ms = 1500) const {
        return wd_.alive(now_ms, timeout_ms);
    }
    // Hand off a newly-received command exactly once; false if none is pending.
    bool takeCommand(Setpoint& out) {
        if (!newCommand_) return false;
        out = lastCommand_;
        newCommand_ = false;
        return true;
    }
    uint32_t lastCommandMs() const { return lastCommandMs_; }

private:
    void send(mavlink_message_t& msg);          // non-blocking write

    Stream*  io_     = nullptr;
    uint8_t  sysid_  = 1;
    uint8_t  compid_ = MAV_COMP_ID_AUTOPILOT1;
    LinkWatchdog      wd_;
    mavlink_message_t rxMsg_{};
    mavlink_status_t  rxStatus_{};
    Setpoint lastCommand_{};
    uint32_t lastCommandMs_ = 0;
    bool     newCommand_    = false;
};
