// ============================================================================
//  board.h  --  Hardware pin map & board constants
//  Adafruit Feather STM32F405 Express (#4382)
//  Pins verified against Adafruit's published pinout.
// ============================================================================
#pragma once

#include <Arduino.h>

// ---- Status LED -------------------------------------------------------------
// Red LED next to the USB jack. On this core LED_BUILTIN is defined, but we
// pin it explicitly to PC1 to be unambiguous.
#ifndef LED_BUILTIN
#define LED_BUILTIN PC1
#endif
static const uint8_t PIN_LED = LED_BUILTIN;

// ---- Onboard NeoPixel (RGB status LED) --------------------------------------
// The mini NeoPixel next to the reset button, on PC0. Used as a dim status
// indicator (green = healthy, red = fault, amber = booting). Fully dimmable via
// its brightness setting, unlike the red LED (which has no PWM).
static const uint8_t PIN_NEOPIXEL = PC0;
static const uint8_t NEOPIXEL_BRIGHTNESS = 4;   // 0..255; keep it dim (~1.5%)

// ---- I2C (BNO085 via STEMMA QT) ---------------------------------------------
// SDA = PB7, SCL = PB6. Board has 10K pullups to 3.3V on both lines.
// The STM32duino default `Wire` maps to these on this board, so we just use
// Wire directly; these are documented here for reference.
static const uint8_t PIN_I2C_SDA = PB7;
static const uint8_t PIN_I2C_SCL = PB6;

// ---- Serial / console -------------------------------------------------------
// `Serial` = native USB-C CDC (console + Jetson link during bring-up).
// Serial3 (PB10 TX / PB11 RX) is reserved for the flight-time Jetson UART link.
static const uint32_t CONSOLE_BAUD = 115200;

// ---- Loop timing ------------------------------------------------------------
// Phase 0 just prints; we still establish the fixed-rate scheduler pattern so
// later phases drop straight in. 50 Hz is plenty for a print/telemetry task.
static const uint32_t PRINT_HZ = 50;
