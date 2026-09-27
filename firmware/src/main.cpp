// ============================================================================
//  main.cpp  --  Phase 1 firmware: IMU + MAVLink link to the Jetson
//
//  Goals (still NO motors, NO props):
//    1. Read the BNO085 (attitude + body rates), as in Phase 0.
//    2. Speak MAVLink to the Jetson over the USB-C CDC link:
//         - HEARTBEAT   @ 1 Hz   (so the Jetson sees us alive)
//         - ATTITUDE    @ 50 Hz  (live telemetry)
//         - STATUSTEXT           (human-readable boot status)
//       and poll inbound Jetson heartbeats to drive the link watchdog.
//    3. Reflect IMU + link health on the status pixel.
//
//  IMPORTANT: USB-C now carries MAVLink ONLY. Do not Serial.print() plain
//  text here -- it would corrupt the byte stream the Jetson is parsing. Send
//  human-readable messages as MAVLink STATUSTEXT instead.
//
//  Verify: on the Jetson run `uv run python -m mavlink.monitor --port
//  /dev/ttyACM0` -> "link UP" + live attitude that tracks board tilt.
//  See ARCHITECTURE.md, Phase 1.
// ============================================================================
#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_NeoPixel.h>

#include "board.h"
#include "command_arbiter.h"
#include "imu.h"
#include "link.h"
#include "rate_timer.h"

static Imu            imu;
static Link           link;
static CommandArbiter arbiter;      // Jetson command vs. failsafe -> active setpoint

// Map the arbiter's flight mode to a MAVLink HEARTBEAT system_status so the
// Jetson can see it: IDLE=STANDBY, NORMAL=ACTIVE, FAILSAFE=CRITICAL.
static uint8_t statusForMode(Mode m) {
    switch (m) {
        case Mode::NORMAL:   return MAV_STATE_ACTIVE;
        case Mode::FAILSAFE: return MAV_STATE_CRITICAL;
        default:             return MAV_STATE_STANDBY;   // IDLE
    }
}

// Onboard RGB status LED (1 pixel on PC0). Kept dim via setBrightness().
static Adafruit_NeoPixel pixel(1, PIN_NEOPIXEL, NEO_GRB + NEO_KHZ800);

// Set the status pixel to a color (0..255 each). Brightness is capped globally
// by NEOPIXEL_BRIGHTNESS so these stay dim.
static void statusColor(uint8_t r, uint8_t g, uint8_t b) {
    pixel.setPixelColor(0, pixel.Color(r, g, b));
    pixel.show();
}

// Fixed-rate schedulers (RateTimer lives in rate_timer.h so it is host-testable).
static RateTimer attitudeTimer(PRINT_HZ);   // 50 Hz telemetry
static RateTimer heartbeatTimer(1);         // 1 Hz heartbeat
static RateTimer ledTimer(10);              // 10 Hz status-pixel update

static bool imuOk = false;

// ---- I2C bus scan (diagnostic) ---------------------------------------------
// Returns the number of devices that ACKed. Reported to the Jetson via
// STATUSTEXT (no plain-text console output on the MAVLink link).
static uint8_t i2cScan() {
    uint8_t found = 0;
    for (uint8_t addr = 1; addr < 127; ++addr) {
        Wire.beginTransmission(addr);
        if (Wire.endTransmission() == 0) ++found;
    }
    return found;
}

void setup() {
    pinMode(PIN_LED, OUTPUT);
    digitalWrite(PIN_LED, LOW);         // red LED off; NeoPixel is the status light

    // Bring up the status pixel first and show a dim amber "booting" color.
    pixel.begin();
    pixel.setBrightness(NEOPIXEL_BRIGHTNESS);
    statusColor(255, 120, 0);           // dim amber = booting

    Serial.begin(CONSOLE_BAUD);
    // Wait briefly for the USB CDC host to attach, but never block forever
    // (the board must run headless when flying).
    uint32_t t0 = millis();
    while (!Serial && (millis() - t0) < 3000) { /* spin */ }

    // From here on, Serial carries MAVLink only.
    link.begin(Serial);

    Wire.begin();
    Wire.setClock(400000);              // 400 kHz fast mode
    const uint8_t nDev = i2cScan();

    imuOk = imu.begin();

    // Announce boot status to the Jetson. Harmless if nobody is listening yet
    // (the message is simply dropped when the TX buffer isn't drained).
    char buf[50];
    snprintf(buf, sizeof(buf), "boot: i2c=%u dev, imu=%s",
             (unsigned)nDev, imuOk ? "OK" : "FAIL");
    link.sendStatusText(imuOk ? MAV_SEVERITY_INFO : MAV_SEVERITY_ERROR, buf);

    // Reflect IMU init result on the status pixel (link not up yet).
    statusColor(imuOk ? 0 : 255, imuOk ? 255 : 0, 0);  // dim green = OK, red = fault
}

void loop() {
    const uint32_t now = millis();

    // Pump the IMU as fast as possible so no reports back up, and service
    // inbound MAVLink (Jetson heartbeats + setpoints) every iteration.
    if (imuOk) imu.update();
    link.poll(now);

    // Feed any freshly received Jetson command into the arbiter, then recompute
    // the active setpoint from link health. On link/command loss this yields the
    // §4 failsafe (level + gentle descent) -- never the last commanded value.
    Setpoint cmd;
    if (link.takeCommand(cmd)) arbiter.command(link.lastCommandMs(), cmd);
    const Mode mode = arbiter.update(now, link.jetsonAlive(now));

    // 1 Hz heartbeat; system_status carries the current flight mode.
    if (heartbeatTimer.due(now)) {
        link.sendHeartbeat(statusForMode(mode));
    }

    // 50 Hz telemetry: attitude up, plus the active setpoint we're applying.
    if (attitudeTimer.due(now)) {
        if (imuOk && imu.healthy()) link.sendAttitude(imu.lastUpdateMs(), imu.attitude(), imu.rates());
        link.sendAttitudeTarget(now, arbiter.active());
    }

    // Status pixel reflects IMU health + flight mode. Fault always wins (red).
    // NOTE: keep structured flow (no early `return`) -- later phases add
    // motor-output code after this block.
    if (ledTimer.due(now)) {
        const bool imuHealthy = imuOk && imu.healthy();
        if (!imuHealthy) {
            statusColor(255, 0, 0);         // dim red   = IMU fault/stale
        } else if (mode == Mode::NORMAL) {
            statusColor(0, 255, 0);         // dim green = following Jetson command
        } else if (mode == Mode::FAILSAFE) {
            statusColor(255, 120, 0);       // dim amber = link lost -> failsafe
        } else {
            statusColor(0, 0, 255);         // dim blue  = IDLE (awaiting command)
        }
    }
}
