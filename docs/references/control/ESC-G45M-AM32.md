# Identified ESC: Flywoo GOKU G45M AM32

The user identified the installed model using this
[GetFPV product listing](https://www.getfpv.com/flywoo-goku-g45m-45a-3-6s-am32-4-in-1-esc-20x20.html)
on September 27, 2026. The product identity is now established by the user;
installed firmware version and saved settings remain unverified. The user
subsequently confirmed the signal rewiring and installation; actual
channel-to-motor response remains to be checked.

## Manufacturer specifications relevant to control

Flywoo lists a 45 A, 3–6S, four-in-one AM32 ESC with 20×20 mm mounting,
PWM and DShot support, including DShot300/600/1200. The advertised 24–128 kHz
PWM frequency describes motor-drive switching, not the input pulse refresh
rate we generated on the STM32.

Sources: [Flywoo G45M](https://www.flywoo.net/products/goku-g45m-32bit-128k-2-6s-45a-esc),
[AM32 PWM frequency explanation](https://wiki.am32.ca/general/Recommended-Settings-For-Freestyle.html).

## What this changes about the incident

The model supports PWM; the motor-4 incident cannot be attributed simply to an
unsupported input protocol. AM32 loads PWM low/high/neutral thresholds from
saved settings and processes reversible and ordinary throttle modes differently.
Therefore the product name alone does not establish how this unit interpreted
1000 µs. A settings mismatch is one possible explanation, not a diagnosis.

The unpowered STM32 pad measurements do not verify the physical routing from
Feather outputs to ESC inputs, nor reproduce the powered stop failure.

Source reviewed: AM32 commit `55c96847a0cddfee9852eb65d2b10e58f563b3d7`,
[settings load](https://github.com/am32-firmware/AM32/blob/55c96847a0cddfee9852eb65d2b10e58f563b3d7/Src/main.c),
[PWM input decoding](https://github.com/am32-firmware/AM32/blob/55c96847a0cddfee9852eb65d2b10e58f563b3d7/Src/signal.c).
This is upstream reference code, not proof of the firmware installed on this ESC.

## Recommended driver direction

Use DShot300 for the next STM32-to-ESC driver. Keep MAVLink for the
Jetson/host-to-STM32 commands and telemetry:

```text
Jetson / host -- MAVLink --> STM32 -- DShot300 --> G45M ESC --> motors
```

DShot uses digital throttle values and a checksum, avoiding PWM endpoint
calibration. Upstream AM32 handles a valid DShot value of zero as zero input.
That is a protocol property; successful physical stop still requires verification.
DShot does not correct swapped signal wires or establish the physical motor map.

Sources: [DShot specification](https://betaflight.com/docs/wiki/guides/current/Dshot),
[AM32 DShot decoder](https://github.com/am32-firmware/AM32/blob/55c96847a0cddfee9852eb65d2b10e58f563b3d7/Src/dshot.c).

Before powered tests resume:

1. Verify channel-to-motor response against the user-confirmed Feather 9/10/6/5 wiring.
2. Read and record the ESC firmware identity and settings. The present custom STM32
   firmware does not provide AM32 configurator passthrough.
3. Verify the new [DShot + IMU integration](../../../firmware/DSHOT.md) on hardware.
   Bounded commands, finite duration, command expiry and separate IMU freshness
   checks are implemented in source. The zero-throttle IMU signal profile has
   been flashed and verified by readback, with live checks pending. Validate
   these behaviors on unpowered ESC input signals before enabling a powered test.
4. Establish physical stop behavior before attempting a low-throttle spin.

Keep the ESC battery disconnected during wiring checks. The original STM32 IMU
firmware is preserved in verified backups, and host spin requests remain blocked. Do not select
an ESC firmware image solely from the retailer's firmware-label text.
