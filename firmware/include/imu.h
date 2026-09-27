// ============================================================================
//  imu.h  --  BNO085 attitude & rate source (behind a clean interface)
//
//  The rest of the firmware talks only to this interface, never to the
//  Adafruit_BNO08x / SH-2 details. That keeps the control code portable and
//  makes the IMU independently testable.
// ============================================================================
#pragma once

#include <Arduino.h>
#include "imu_samples.h"

// Euler angles in degrees; body angular rates in deg/s.
struct Attitude {
    float roll_deg;
    float pitch_deg;
    float yaw_deg;
};

struct Rates {
    float roll_dps;   // gyro x (body)
    float pitch_dps;  // gyro y (body)
    float yaw_dps;    // gyro z (body)
};

class Imu {
public:
    // Initialise the BNO085 over I2C and enable the reports we need
    // (game rotation vector for attitude, calibrated gyro for rates).
    // Returns false if the sensor is not found / fails to configure.
    bool begin();

    // Pump the sensor: read any pending SH-2 reports and update cached values.
    // Call every loop iteration. Updates `lastUpdateMs` when new data arrives.
    void update();

    // Latest fused attitude (game rotation vector; yaw is relative and drifts).
    Attitude attitude() const { return attitude_; }

    // Latest calibrated body angular rates.
    Rates rates() const { return rates_; }

    // Health: true if we've had a fresh sample within `timeoutMs`.
    bool healthy(uint32_t timeoutMs = 100) const {
        return configured_ && samples_.healthy(millis(), timeoutMs);
    }
    bool attitudeFresh() const { return configured_ && samples_.attitudeFresh(millis()); }
    bool gyroFresh() const { return configured_ && samples_.gyroFresh(millis()); }
    uint32_t resetCount() const { return resets_; }
    // Boot diagnostics: 0 not tried, 1 no address, 2 both addresses,
    // 3 device/SH2 init failed, 4 report setup failed, 5 configured.
    uint8_t initStatus() const { return initStatus_; }
    uint8_t address() const { return address_; }
    uint8_t addressMask() const { return addressMask_; } // bit0: 4A, bit1: 4B
    uint32_t attitudeReports() const { return attitudeReports_; }
    uint32_t gyroReports() const { return gyroReports_; }
    uint32_t rejectedReports() const { return rejectedReports_; }

    // Raw quaternion accessors (w,x,y,z) if a consumer wants them directly.
    void quaternion(float &w, float &x, float &y, float &z) const {
        w = qw_; x = qx_; y = qy_; z = qz_;
    }

    uint32_t lastUpdateMs() const { return samples_.attitudeAt(); }
    uint32_t gyroUpdateMs() const { return samples_.gyroAt(); }

private:
    void applyQuaternion(float w, float x, float y, float z);

    Attitude attitude_{0, 0, 0};
    Rates    rates_{0, 0, 0};
    float    qw_ = 1, qx_ = 0, qy_ = 0, qz_ = 0;
    ImuSamples samples_;
    uint32_t resets_ = 0;
    bool configured_ = false;
    uint8_t initStatus_ = 0, address_ = 0, addressMask_ = 0;
    uint32_t attitudeReports_ = 0, gyroReports_ = 0, rejectedReports_ = 0;
};
