# Corvidia — final compact frame

## Print file

[Corvidia_Compact_P2S_PLA_Motor_Holes_Fixed.3mf](output/Compact_P2S_PLA/Corvidia_Compact_P2S_PLA_Motor_Holes_Fixed.3mf)

Open as a project in Bambu Studio. Configured for **Bambu P2S, 0.4 mm standard nozzle, Generic PLA and Textured PEI**. Keep the saved placement at 100% scale; do not auto-arrange.

- One connected part on one 256 × 256 mm plate.
- Approximately 6h 4m and 185 g PLA, including supports and brim.
- Flat 110 × 116 mm center for the user's 95 × 111 mm tray.
- Arms taper from 28 mm roots to 20 mm ends; 6 mm deck and motor pads.
- Four motor mounts and a braced, forward-facing camera bracket.
- Verified open motor screw and center relief holes in the sliced paths.

See the [print and assembly guide](output/Compact_P2S_PLA/START_HERE.md) for hardware, placement and physical checks.

## Files kept

| Location | Contents |
|---|---|
| `output/Compact_P2S_PLA/` | Final print project, FreeCAD, STEP, STL, guide, previews and current verification records |
| `output/Corvidia_Compact_One_Print.zip` | Final package, including CAD sources, profiles and component references |
| `cad/` | Final build, slicing and verification scripts; shared geometry definitions and parameters |
| `cad/profiles/` | P2S machine, Generic PLA and final process settings |
| `references/` | Component source geometry/drawings needed to understand and rebuild the design |

## Edit and rebuild

Run `cad/Open_Compact.FCMacro` in FreeCAD to open the editable model with its Python definitions loaded. The model uses named spreadsheet parameters and procedural solids.

From this workspace on this Mac:

```sh
PYTHONPATH=/Applications/FreeCAD.app/Contents/Resources/lib /Applications/FreeCAD.app/Contents/Resources/bin/python cad/compact_frame.py
python3 cad/slice_compact.py
python3 cad/check_compact.py
```

The slicer script defaults to `/Applications/BambuStudio.app`; use `--app` to specify another installation. Python verification requires Pillow. `cad/prepare_references.py` regenerates the Orin cache from the retained manufacturer STEP if needed.

Rebuilding replaces generated outputs and creates temporary files under `tmp/`. The saved print project and CAD have passed software checks; physical tray fit, adhesive, motor screw engagement, assembled balance, temperatures, strength and bench restraint still require hardware checks.
