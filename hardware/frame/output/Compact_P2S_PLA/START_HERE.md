# Corvidia Compact — flat center for your own tray

Open **`Corvidia_Compact_P2S_PLA_Motor_Holes_Fixed.3mf` as a project** in Bambu Studio.

- Bambu Lab P2S, standard 0.4 mm nozzle, Generic PLA, Textured PEI.
- One connected part on one 256 × 256 mm plate.
- Part envelope: approximately **240 × 224 × 48 mm**.
- Flat bonding area: **110 × 116 mm**, centered on X=0, Y=0, top Z=6.
- Print estimate: **6 hours 4 minutes / 185 g PLA**, including support and brim.
- Actual extrusion footprint including brim/support: approximately **245.3 × 229.3 mm**, inside the 256 × 256 bed.

## Flat center

The center is a solid 6 mm plate, level with the arm roots. It has **no Jetson posts, edge stops, screw holes, bench bosses or ribbon slot**. Its full deck is 116 × 116 mm; the camera mount at the nose leaves a centered 110 × 116 mm rectangle completely unobstructed. Glue your existing tray to this surface, with the Jetson centered between the motors.

Your measured tray footprint is **95 × 111 mm**. Orient it with 95 mm along X (front to rear) and 111 mm along Y (left to right), centered at (0,0). This leaves 7.5 mm per end and 2.5 mm per side within the flat rectangle. Its thickness, board support height and adhesive are unmeasured; the complete fit and attachment have not been physically validated. The Jetson CAD reference is shown at a **provisional PCB underside height of Z=14**, assuming 8 mm lift above the deck. It is a placement reference, not a model of your tray. Check the real tray and board against the camera bracket, connector access and propeller sweeps before bonding. The frame provides a bonding surface for the tray; it does not directly support a bare Jetson PCB.

The four arms now taper from **28 mm-wide rounded roots to 20 mm-wide ends**, replacing the earlier 16 mm arms. Their thickness remains 6 mm, level with the deck, and motor mounting height and screw engagement are unchanged. The wider roots add material at the deck joints; structural strength and vibration performance have not been physically tested. The four motors, small-board mounting tabs and braced camera mount remain. The camera stays forward-facing at 0°, with two 3 mm triangular braces and an open window. No guards or canopy are included.


## Print

1. Open the supplied 3MF as a project and keep its placement. **Do not auto-arrange**: the centered part's actual brim and support toolpaths have been checked against the bed. Keep scale at 100%.
2. Confirm P2S, standard 0.4 mm nozzle, Generic PLA and Textured PEI. This is one single-color job; no AMS is needed.
3. Embedded settings: 0.24 mm layers, 0.20 mm first layer, four walls, 100% rectilinear infill, four top/bottom layers, 3 mm outer brim and automatic organic tree supports. The integrated motor pads remain solid.
4. Starting profile temperatures are 220°C nozzle / 55°C bed. Match the actual filament and re-slice if changing settings. Review the sliced preview before printing.
5. Cool the plate before removal. Remove the brim and camera supports and clear the mounting bores.

No print has been sent to a printer.

## Mount the hardware

X is forward, Y left and Z up. The bottom face is Z=0.

| Component | Position / interface | Hardware |
|---|---|---|
| Four motors | Centers (±104, ±96); 11.5 × 11.5 pattern; Ø3.4 holes; 6 mm pads | Four M3 screws per motor from below. Length = pad thickness + measured safe engagement. |
| Existing 95 × 111 Jetson tray | Centered 110 × 116 flat area, top Z=6 | User-supplied tray and adhesive; no printed Jetson mounts. |
| ESC | Center (0,-75); 20 × 20 pattern; nominal PCB underside Z=10 | M2 through bolts, supplied grommets and nuts; match length to actual stack. |
| Feather | Center (0,+73); 45.72 × 17.78 pattern; PCB underside Z=10 | M2 × 14 starting length, nuts and insulating washers; verify fit. |
| IMU | Center (-45,-73); 20.32 × 17.78 pattern; PCB underside Z=10 | M2 × 14 starting length, nuts and insulating washers; verify board revision. |
| B0249 camera | Mount front face X=58; optical center Z=26; 34 × 34 pattern | M2 × 8 starting length through 3 mm bracket; check the actual PCB, washers and nuts. |

**The old four M4 bench holes are removed.** The earlier 80 × 40 drilling pattern does not apply to this revision. Your manual fixture must restrain the frame through a separate attachment; the glued electronics tray is not a specified motor-test restraint.

Use the paired arm holes to tie motor wires. Route the camera ribbon around the edge of the flat deck; its former through-slot is removed. Secure external power strain relief to the bench base and leave slack to avoid pulling on the tray. Keep the fan and connectors clear. Provide space beneath the frame for motor screw heads and small-board nuts.

## Verification

The mesh export now shares vertices between adjacent triangles. This corrects a previous 3MF export that caused the slicer to fill holes. All 16 motor screw bores and four central relief bores are checked against the actual extrusion paths on every motor-pad layer, including after reopening and re-slicing. Use the file marked `Motor_Holes_Fixed`.

The automated report checks a valid single solid, watertight connected mesh, nominal component/prop/fan/camera/connector clearances and bed fit. It also verifies that the entire 110 × 116 mm bonding rectangle contains solid material below Z=6 and no raised features above it. Saved FreeCAD parameter edits and the Bambu project's re-slice are checked separately.

The supplied 95 × 111 mm tray footprint fits the flat rectangle. These checks do not validate its unmodeled height and details, adhesive, actual assembled balance, physical fit, temperatures or strength. Keep the restrained bench-demo use and physical hardware checks from the previous design: motor screw engagement, wiring clear of props, actual installed blade height, camera depth and PLA performance remain to be checked on hardware. The camera depth and propeller Z envelope are provisional allowances.

## Files and rebuilding

- `Corvidia_Compact_P2S_PLA_Motor_Holes_Fixed.3mf`: prepared Bambu print project.
- `Corvidia_Compact.FCStd`, `.step`, `.stl`: CAD and geometry exports.
- `geometry_checks.json`, `print_checks.json`, `reopen_check.json`, `editability_check.json`: current verification records.
- `preview.png`, `first_layer.png`, `toolpaths.png`: current slice previews.

Run `cad/Open_Compact.FCMacro` from the workspace to load the FreeCAD Python definitions and open the editable parameter sheet. Rebuild with `cad/compact_frame.py`, `cad/slice_compact.py`, then `cad/check_compact.py`. A full rebuild uses the original manufacturer references retained in the workspace. The Jetson reference height can be adjusted with `JetsonGap` in the source defaults and rebuilt after your tray is measured.

Profiles derive from [Bambu Studio 2.8.2.61](https://github.com/bambulab/BambuStudio/releases/tag/v02.08.02.61), retaining the official P2S machine routines. The official end routine includes `T65535`; the CLI logs it as an invalid T command while completing the slice successfully. Machine G-code has not been altered.
