# Laptop evaluation harness

Runs the perception pipeline on a laptop against public datasets, without the Jetson, and
documents the datasets and results behind the `perception-pipeline` branch. On the Jetson the
same pipeline order is one command (see the main README, "Free-space stream"):

```sh
uv run corvidia-run --camera --freespace-hz 10 --stub-delay 0   # depth thread + YOLO + gate + evidence, no Cosmos
```

What is real here and what is substituted:

| Stage | Jetson | Laptop |
|---|---|---|
| Source | Argus CSI camera or recorded clip | ingested clip directory (same format as `corvidia-record`) |
| Free-space stream | `FreeSpaceStream` + Depth Anything V2 Metric Small on TensorRT | same `FreeSpaceStream`, same model via transformers on CPU |
| Detector | YOLO11n FP16 TensorRT engine | same YOLO11n weights, Ultralytics on CPU |
| Tracker, gate, queue, worker, evidence, best shot | unchanged | unchanged |
| Confirmer | Cosmos (optional) | stub `--confirmer yes` (also the Jetson default) |

## Setup

```sh
cd perception/laptop-eval
./setup.sh              # .venv with CPU torch, ultralytics, opencv, transformers; yolo11n.pt; depth checkpoint
./fetch_datasets.sh     # DanceTrack (3 smallest clips) and MOT17-05, ingested into clips/
```

## Datasets

Used for the results below (fetched by `fetch_datasets.sh`):

| Dataset | Hugging Face id | What it tests | License | Size fetched |
|---|---|---|---|---|
| DanceTrack | `Voxel51/DanceTrack` | 25 fps 1080p mp4, per-frame boxes with identities, head-height, indoor and outdoor groups | CC BY 4.0 | 3 clips, ~23 MB + 83 MB annotations |
| MOT17-05 | `Lekim89/MOT17` (mirror of MOTChallenge) | 14 fps 640x480, moving street-level camera, 88 annotated people, ignore regions and distractor classes | upstream CC BY-NC-SA 3.0 (research use only) | one sequence, ~30 MB |

Other options verified in the 2026-09-27 dataset sweep (not fetched by default):

| Dataset | Hugging Face id | Fit | License |
|---|---|---|---|
| VisDrone2019-MOT | `huseyincavus/visdrone2019-mot` | real UAV footage, oblique from tens of metres, people small; 1.64 GB val split | CC BY-NC 4.0 |
| P-DESTRE | `2uanDM/p-destre` | DJI at 5.5-6.7 m, 45-90 degree pitch, closest to a quadcopter view; 30 fps 4K mp4 with identities; selective download from a 72 GB tar | uploader tag Apache-2.0, upstream research use |
| UAV123 (person sequences) | `xche32/UAV123` | drone following one person at 5-25 m, 30 fps; single 14 GB tarball | not stated |
| Print attack sample | `AxonData/print-attack-dataset` | 15 clips of a camera on a printed photo of a person: hard negative for the confirmer, runs through the whole pipeline | CC BY 4.0 |
| Replay attack sample | `AxonData/Replay_attack_mobile` | 39 clips of a phone filming a screen showing a person: hard negative | CC BY 4.0 |
| PA-100K | `tuandunghcmut/PA-100K` | 100k real surveillance person crops for confirmer positives | CC BY 4.0 |
| COCO 2017 person val | `naufalso/coco-2017-person-val` | still images, detector-only precision/recall | COCO terms |

Any MOTChallenge-style sequence (`img1/`, `seqinfo.ini`, `gt/gt.txt`), FiftyOne video export, or
plain video with a MOT `gt.txt` ingests with `tools/ingest.py`. Clip names must not contain a dot.

## Ingest, run, score

```sh
V=.venv/bin/python
$V tools/ingest.py mot data/mot17/train/MOT17-05-FRCNN --out clips/mot17-05
$V tools/ingest.py fiftyone data/dancetrack/data/dancetrack0008.mp4 \
    --frames data/dancetrack/frames.json --samples data/dancetrack/samples.json --out clips/dancetrack0008
$V tools/ingest.py video some_clip.mp4 --out clips/print-attack-01       # spoof clip, no gt

./run_clip.sh dancetrack0008              # YOLO-alone arm: accuracy replay -> autolabel -> score
./run_clip.sh print-attack-01 --depiction # every track is a depiction, no people
```

Clips are `000000.jpg...` plus `frames.jsonl` with `capture_ts = index / fps` from the dataset's
true frame rate (so the gate's 1 s window is right at 14 fps), `gt.txt` in MOT format, and
`clip.json` with provenance. Frames are re-encoded once at quality 95 at native resolution;
`--max-side` exists only to emulate a camera output size (YOLO letterboxes to 640, depth resizes
to 518x294, and confirmer crops are capped at 448 px regardless).

`tools/autolabel.py` turns `gt.txt` into the `corvidia-encounter-eval` label file: each track's
first event box is matched at its frame by IoU (0.5 person, 0.2 partial, overlap with a MOT
distractor or reflection row = depiction, else not_person). Tracks it will not guess at are left
unlabeled and listed by the scorer: overlaps with ignore regions (conf 0), unmatched boxes at the
frame edge (on MOT17-05 every one was a real person entering or leaving that the annotators did
not box), and frames with no gt. Review those with `corvidia-encounter-eval sheets` and label by
hand; hand labels are never overwritten. A clip's "people" are gt identities with 15+ frames at
visibility >= 0.5, height >= 48 px and width >= 24 px (the crop gate's minimums).

## The pipeline order with the free-space stream

```sh
$V tools/mimic_pipeline.py clips/mot17-05 --events runs/mot17-05 --mode realtime --depth-hz 10
# emulate compute 4x faster than the laptop: replay 4x slower and scale every time constant 4x
$V tools/mimic_pipeline.py clips/mot17-05 --events runs/mot17-05 --mode realtime \
    --speed 0.25 --depth-hz 2.5 --stale-s 0.6 --control-hz 5 --grace-s 2.0
```

Add `--freespace-udp 127.0.0.1:5601 --tracks-udp 127.0.0.1:5602` to feed the bridge in
[`../bridge/`](../bridge/README.md) from the laptop (verified 2026-09-27 with the framework's
broker: 110 track messages, 27 free-space profiles, 11 capture events and healthy heartbeats
reached the bus during a DanceTrack replay).

This drives the packaged `FreeSpaceStream` with the CPU depth model and replays a virtual 20 Hz
controller against `freespace.jsonl`: the fraction of ticks that saw free space younger than the
consumer's rule (150 ms, from the autodrone framework's L3/L4 guards) under both stamping choices,
and how often the L4 guard (stale for 0.5 s) would have fired.

## Results, 2026-09-27

YOLO-alone arm, accuracy replay (every frame, laptop CPU):

| Clip | People | Tracks | False | Precision | Found | Duplicate events | Unlabeled |
|---|---|---|---|---|---|---|---|
| DanceTrack 0008 | 8 | 26 | 0 | 1.00 | 8/8 | 18 | 0 |
| MOT17-05 | 52 | 132 | 3 | 0.975 | 46/52 | 30 | 11 |

Free-space stream + detector, real time (laptop CPU; both PyTorch models contend for the same
cores) and 4x dilated (video-time equivalents, gate/queue windows scaled):

| Run | YOLO fps | Depth Hz (target 10) | Depth inference | Fresh ticks, stamp at publish / capture | Guard stops | Events | Found | Precision |
|---|---|---|---|---|---|---|---|---|
| DanceTrack, real time 1x | 5.5 | 1.2 | 596 ms | 18 % / 0 % | 14 / 1 | 6 | 5/8 | 1.00 |
| MOT17-05, real time 1x | 4.5 | 1.9 | 480 ms | 28 % / 0 % | 3 / 1 | 2 | 2/52 | 1.00 |
| DanceTrack, 4x dilated | 17.0 | 4.6 | 157 ms | 68 % / 0 % | 0 / 1 | 20 | 8/8 | 1.00 |
| MOT17-05, 4x dilated | 14.0 (0 dropped) | 5.0 | 139 ms | 72 % / 0 % | 0 / 2 | 129 | 46/52 | 0.983 |

Reading: at 1x the laptop cannot run both models at rate, the detector starves the gate and the
guard would stop the mission repeatedly. With 4x more compute the detector keeps up, events and
recall return to accuracy-mode levels, and the structure meets the guard only when free space is
stamped at publish time; stamping at capture counts the map's own age (~210-240 ms here, ~56 ms
with the Jetson engine) and fails every tick. Depth reaches half its target because this CPU's
inference is 140-160 ms video-time against the engine's 46 ms. Duplicate events are cross-track
fragmentation (one person, several track ids), the dominant open issue. Depth is uncalibrated
(`scale` 1.0); MOT17-05 is 4:3, so its depth input was stretched by a third.

The JSON reports behind both tables are in `results/2026-09-27/`.
