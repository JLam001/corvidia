// ============================================================================
//  imu.cpp  --  BNO085 driver wrapper implementation
// ============================================================================
#include "imu.h"
#include "board.h"

#include <Adafruit_BNO08x.h>
#include <Wire.h>

// The Adafruit lib pulls in the CEVA SH-2 headers; report-id constants
// (SH2_GAME_ROTATION_VECTOR, SH2_GYROSCOPE_CALIBRATED, ...) and sh2_SensorValue_t
// come from there.

namespace {
    // Reset pin is optional on the STEMMA QT wiring (-1 = not connected).
    Adafruit_BNO08x bno(-1);
    sh2_SensorValue_t sensorValue;

    // Report intervals (microseconds). 5000us = 200 Hz. The BNO085 supports
    // higher; 200 Hz is a comfortable, reliable default for bring-up.
    const uint32_t ROTVEC_INTERVAL_US = 5000;   // 200 Hz attitude
    const uint32_t GYRO_INTERVAL_US   = 5000;   // 200 Hz rates

    // Enable the reports we care about; small retry so a transient I2C hiccup
    // at boot doesn't leave us with no data.
    bool enableReports() {
        bool ok = true;
        ok &= bno.enableReport(SH2_GAME_ROTATION_VECTOR, ROTVEC_INTERVAL_US);
        ok &= bno.enableReport(SH2_GYROSCOPE_CALIBRATED, GYRO_INTERVAL_US);
        return ok;
    }
}

bool Imu::begin() {
    samples_.reset();
    configured_ = false;
    initStatus_ = address_ = addressMask_ = 0;
    // Allow a cold-powered sensor to boot. Probe both documented addresses;
    // never silently select a device when both acknowledge.
    delay(500);
    for (unsigned attempt = 0; attempt < 3; ++attempt) {
        addressMask_ = 0;
        for (unsigned bit = 0; bit < 2; ++bit) {
            Wire.beginTransmission(uint8_t(0x4a + bit));
            if (Wire.endTransmission() == 0) addressMask_ |= 1u << bit;
        }
        if (addressMask_) break;
        delay(100);
    }
    if (addressMask_ == 0) { initStatus_ = 1; return false; }
    if (addressMask_ == 3) { initStatus_ = 2; return false; }
    address_ = addressMask_ == 1 ? 0x4a : 0x4b;
    const bool found = bno.begin_I2C(address_, &Wire);
    // BusIO calls Wire.begin(), which resets this core to 100 kHz. Restore
    // fast mode before report configuration and normal sampling.
    Wire.setClock(400000);
    if (!found) {
        initStatus_ = 3;
        return false;
    }
    // Give the hub a moment, then enable reports (retry a couple of times).
    for (int attempt = 0; attempt < 3; ++attempt) {
        if (enableReports()) {
            configured_ = true;
            initStatus_ = 5;
            return true;
        }
        delay(20);
    }
    initStatus_ = 4;
    return false;
}

void Imu::applyQuaternion(float w, float x, float y, float z) {
    const float scale = 1.0f / sqrtf(w*w + x*x + y*y + z*z);
    w *= scale; x *= scale; y *= scale; z *= scale;
    qw_ = w; qx_ = x; qy_ = y; qz_ = z;

    // Quaternion -> Euler (aerospace ZYX: yaw-pitch-roll), degrees.
    const float sinr_cosp = 2.0f * (w * x + y * z);
    const float cosr_cosp = 1.0f - 2.0f * (x * x + y * y);
    attitude_.roll_deg = atan2f(sinr_cosp, cosr_cosp) * RAD_TO_DEG;

    float sinp = 2.0f * (w * y - z * x);
    if (sinp > 1.0f)  sinp = 1.0f;      // clamp for asinf domain
    if (sinp < -1.0f) sinp = -1.0f;
    attitude_.pitch_deg = asinf(sinp) * RAD_TO_DEG;

    const float siny_cosp = 2.0f * (w * z + x * y);
    const float cosy_cosp = 1.0f - 2.0f * (y * y + z * z);
    attitude_.yaw_deg = atan2f(siny_cosp, cosy_cosp) * RAD_TO_DEG;
}

void Imu::update() {
    // If the sensor was reset (e.g. brownout), re-enable reports.
    if (bno.wasReset()) {
        samples_.reset();
        ++resets_;
        configured_ = enableReports();
    }
    if (!configured_) return;

    // Bound report draining. An individual library/I2C call may still block;
    // the integrated DShot image has an independent interrupt-driven deadline.
    for (unsigned budget = 0; budget < 4 && bno.getSensorEvent(&sensorValue); ++budget) {
        switch (sensorValue.sensorId) {
            case SH2_GAME_ROTATION_VECTOR: {
                ++attitudeReports_;
                const auto& q = sensorValue.un.gameRotationVector;
                if (samples_.attitude(millis(), sensorValue.sequence, q.real, q.i, q.j, q.k))
                    applyQuaternion(q.real, q.i, q.j, q.k);
                else ++rejectedReports_;
                break;
            }

            case SH2_GYROSCOPE_CALIBRATED:
                ++gyroReports_;
                if (!samples_.gyro(millis(), sensorValue.sequence,
                    sensorValue.un.gyroscope.x, sensorValue.un.gyroscope.y,
                    sensorValue.un.gyroscope.z)) { ++rejectedReports_; break; }
                // BNO reports rad/s; convert to deg/s for the control loop.
                rates_.roll_dps  = sensorValue.un.gyroscope.x * RAD_TO_DEG;
                rates_.pitch_dps = sensorValue.un.gyroscope.y * RAD_TO_DEG;
                rates_.yaw_dps   = sensorValue.un.gyroscope.z * RAD_TO_DEG;
                break;

            default:
                break;
        }
    }

}
