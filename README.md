# Corvidia

Project repository for the Corvidia bench demonstrator.

## Mechanical frame

The final frame, editable CAD, manufacturing references and print setup live in
[`hardware/frame/`](hardware/frame/README.md).

- [Prepared P2S PLA print project](hardware/frame/output/Compact_P2S_PLA/Corvidia_Compact_P2S_PLA_Motor_Holes_Fixed.3mf)
- [Print and assembly guide](hardware/frame/output/Compact_P2S_PLA/START_HERE.md)
- [Complete frame package](hardware/frame/output/Corvidia_Compact_One_Print.zip)

The frame has a flat center for a 95 × 111 mm tray, four reinforced motor arms,
and a braced camera mount. It prints on one 256 × 256 mm plate. Physical hardware
fit and operating checks are documented in the guide.

## Software

- [Onboard mission console](perception/STAND_DEMO.md): search briefs, timed
  collection, saved images, and the tested 5% motor-input demo profile.
- [Perception pipeline](perception/README.md): person detection, tracking, and video replay.
- [Event pipeline design](docs/event-pipeline.md): confirmation and evidence workflow.
- [STM32 firmware import](firmware/IMPORT.md): BNO085 IMU polling, MAVLink telemetry,
  command handling, and host-test results.
- [Motor controls and corrected wiring](firmware/MOTOR_CONTROL.md): individual ESC
  tests, the planned motor layout, and bounded movement proposals.
- [Firmware guide](firmware/README.md): PlatformIO setup, installed v5 bench
  firmware, and the prepared v6 startup ramp.

## Repository layout

```text
docs/             # Software architecture and design
perception/       # Jetson person detection/event pipeline
firmware/         # Feather STM32F405 PlatformIO project
hardware/
  frame/
    cad/          # Parametric CAD, build/check scripts and printer profiles
    references/   # Manufacturer geometry and drawings
    output/       # Final print files, CAD exports and verification records
```

Mechanical files live under `hardware/`; Jetson software and STM32 firmware
are separate projects under `perception/` and `firmware/`.

## Get the complete files

Large CAD and print files use Git LFS. Install Git LFS, then run:

```sh
git lfs install
git clone https://github.com/JLam001/corvidia.git
cd corvidia
git lfs pull
```

For an existing checkout, run `git lfs pull` before opening CAD or print files.
See the [frame README](hardware/frame/README.md) for editing and rebuilding.
