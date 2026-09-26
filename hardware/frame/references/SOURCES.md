# Component reference register

Retrieved 2026-09-26. Dimensions are millimetres. `cad/parameters.json` and the
FreeCAD `Parameters` spreadsheet identify the provenance of each dimension.

| Item | Source and local evidence | Interpretation |
|---|---|---|
| Orin Nano | [NVIDIA manufacturer STEP](https://developer.nvidia.com/downloads/assets/embedded/secure/jetson/orin_nano/docs/jetson_orin_nano_devkit_3d_step_model.zip); `P3766-P3768SKU4-P3767ENVELOPE.stp` | Carrier 100 x 79; cooler top 29.866 above carrier bottom. Native board bounds X=-4..96, Y=-17..62, Z=0..1.57. Selected holes native (0,0), (86,0), (0,58), (86,58), radius 1.375. Other mounting holes also exist. |
| Feather #4382 | [Adafruit Eagle PCB](https://github.com/adafruit/Adafruit-Feather-STM32F405-Express-PCB), `feather.brd` | Mount coordinates X=2.54/48.26, Y=2.54/20.32; 45.72 x 17.78 pitch. Two plain holes and two packaged holes. M2 mounting clearance is a design choice. Assembly envelope 52 x 23 x 10 is retained from the supplied plan. |
| BNO085 #4754 | [Adafruit Eagle PCB](https://github.com/adafruit/Adafruit-BNO08x-PCB), `bno08x.brd` | Mount coordinates X=2.54/22.86, Y=2.54/20.32; 20.32 x 17.78 pitch. Legacy master board reference; confirm physical revision. |
| G45M ESC | [Flywoo specification via GetFPV](https://www.getfpv.com/flywoo-goku-g45m-45a-3-6s-am32-4-in-1-esc-20x20.html) | Plan dimensions 33 x 30 x 6.1 and 20 x 20 M2 grommet mounting. Component is represented as an assembly envelope, not manufacturer CAD. |
| RS2205 | [Readytosky](https://readytosky.com/e_productshow/?271-RS2205-2300kv-CWCCW-Brushless-Motor-271.html=) | Diameter 27.9, published overall length 31.7. Body-only height 24 is a provisional design envelope. The 11.5 square pitch is the user's measurement and takes precedence over generic motor patterns. |
| HQProp 4x4.3x3 V1S | [HQProp](https://www.hqprop.com/hq-durable-prop-4x43x3v1s-2cw2ccw-poly-carbonate-p0184.html) | 101.6 swept diameter, 13 x 5.7 hub, 5 bore. Published hub height does not establish blade Z sweep. |
| B0249 | [Arducam drawing via UCTRONICS](https://www.uctronics.com/download/Amazon/B0249_IMX477_HQ_Camera_for_Jetson_Datasheet.pdf), `B0249_datasheet.pdf`, printed pages 2 and 4 | 38 square PCB, 34 outer mounting pitch, lens diameter 30 x length 31, horizontal FOV 65 degrees. Drawing does not establish complete assembled depth. 45 mm depth from PCB front is provisional. |

## Orin alignment

The Orin manufacturer CAD used by this design is retained in full in this folder.
The preview omits the stock base and its four carrier screws (solid indices
1266..1270), the Wi-Fi module and its screw (1377..1378), and antennas/coax
(1380..1392). These are identified from STEP product order, part labels and bounds:
`330-0266-000_M2_WIFI_MODULE_ASM`, `WIFI_ANTENNA_P3526`, and coax/antenna assemblies.
The NVMe assembly (1271..1376, plus mounting screw 1379) is retained as a conservative
optional storage reference. The fan/heatsink and carrier remain intact. Native
solid bounds and volumes are recorded in `orin_solids.json`. Remove the matching
physical wireless hardware for the wired demonstration.

Transform native points by subtracting (46,22.5,0), rotating -90 degrees around Z,
and translating Z to 14 for the current nominal user-tray placement. This puts rear I/O toward frame -X. The selected mounting
pattern becomes X=-22.5/35.5, Y=-40/46: its centre is (6.5,3), not the board centre.
The build uses the actual manufacturer shape and derives Z from the deck height plus the assumed tray lift. The user tray height remains unmeasured.

`orin_without_base.brep` is a reproducible local cache made by
`cad/prepare_references.py`; it is not a substitute source. Native shape order is
validated before extracting the base. See `sha256.json` for source fingerprints.
