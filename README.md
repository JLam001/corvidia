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

## Repository layout

```text
hardware/
  frame/
    cad/          # Parametric CAD, build/check scripts and printer profiles
    references/   # Manufacturer geometry and drawings
    output/       # Final print files, CAD exports and verification records
```

Mechanical files are grouped under `hardware/`, leaving the repository root
available for project software. No application or firmware code has been added yet.

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
