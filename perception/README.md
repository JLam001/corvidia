# Perception: person detection event pipeline

Implements [`docs/event-pipeline.md`](../docs/event-pipeline.md):

- **Step 1, event core:** typed records, candidate lifecycle, bounded queues, stub
  confirmer, evidence writer, deterministic detection-event tests.
- **Step 2, video replay:** YOLO11n + explicitly configured ByteTrack, every-frame
  and real-time replay, live CSI camera source, MJPEG preview, run reports.
- **Step 3, local Cosmos:** Cosmos Reason 2 2B (Q4_K_M) on llama.cpp behind the
  confirmer interface, a labeled starter crop set, and an evaluation tool.
- The live detector runs the YOLO11n TensorRT engine directly (no PyTorch or
  Ultralytics at runtime) with a NumPy ByteTrack.

## Layout

```text
src/corvidia_perception/
  records.py        # frames, detections, candidates, results, skips
  config.py         # settings from the spec (TOML-loadable), not yet calibrated
  crop.py           # 20% padded crop, size/blur gates, frozen copies
  gate.py           # persistence gate, lifecycle, suppression, retry cooldown
  confirm_queue.py  # bounded FIFO, one entry per track, 2 s expiry
  confirmer.py      # backend protocol, strict answer parsing, stub backends
  worker.py         # evidence commit, one active request, deadline, drain/recover
  evidence.py       # atomic writes, storage limits, interrupted-event recovery
  pipeline.py       # EventPipeline wiring + LatestFrameSlot
  health.py         # counters, gauges, faults
  simulate.py       # threaded run on synthetic detections
  detector.py       # detector factory + tracker wrapper with dropped-frame aging
  trt_detector.py   # YOLO on TensorRT: letterbox, zero-copy managed buffers, NMS
  bytetrack.py      # ByteTrack (NumPy), matches Ultralytics' tracker output
  cosmos.py         # llama-server adapter: JSON-schema answers, cancel + idle check
  cosmos_eval.py    # corvidia-cosmos-eval: accuracy/latency on labeled crops
  sources.py        # video replay (every/realtime) and Argus CSI camera
  preview.py        # annotated MJPEG preview + /health JSON
  run.py            # corvidia-run CLI and run report
```

The detector thread calls `EventPipeline.on_frame(frame, detections)`. The
confirmation worker runs on its own thread (`start()`), or is stepped with
`tick()` in tests using a fake clock.

## Jetson setup (once)

The venv must see the system OpenCV (GStreamer/Argus build) and TensorRT, so it
is created with system site-packages. `pyproject.toml` blocks the pip OpenCV
wheels and pins NumPy < 2 to match the system OpenCV.

```sh
cd ~/corvidia/perception
uv venv --python /usr/bin/python3 --system-site-packages
uv sync --extra detector
mkdir -p ~/models && curl -sL -o ~/models/yolo11n.pt \
  https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n.pt
```

The live pipeline uses the FP16 TensorRT engine (`~/models/yolo11n.engine`) and
never imports PyTorch. The `detector` extra (Ultralytics + PyTorch) is only for
exporting the engine, `.pt` comparisons, and parity tests. Build the engine once
(about 10 minutes):

```sh
deploy/export_yolo.sh
```

PyTorch in the tooling extra comes from the `cu130` index, which has no native
sm_87 (Orin) build; `test_gpu_detections_match_cpu` checks its GPU results.

## Cosmos confirmer

```sh
deploy/cosmos_server.sh start    # fetch pinned weights (sha256-checked), run container
deploy/cosmos_server.sh status
```

llama-server runs in the Jetson AI Lab container (pinned by digest) on
`127.0.0.1:8010`, restarting with the system. It uses the community GGUF
`Kbenkhaled/Cosmos-Reason2-2B-GGUF` (Q4_K_M + F16 vision projector) pinned to a
revision, with `-c 2048 -np 1 -cram 0` to keep RAM headroom.

## Run on the Jetson

From the repository root on the Mac:

```sh
rsync -az --delete --exclude='.git' --exclude='.DS_Store' --exclude='.venv' \
  --exclude='__pycache__' --exclude='.pytest_cache' ./ jlam@192.168.2.2:corvidia/
ssh jlam@192.168.2.2 'cd ~/corvidia/perception && ~/.local/bin/uv run pytest -q'
```

On the Jetson (`cd ~/corvidia/perception`):

```sh
# Accuracy replay: every frame, event timing on video time, instant stub answers
uv run corvidia-run --video /usr/share/opencv4/samples/data/vtest.avi --mode every

# Timing replay: real-time pacing (here 4x) with frame dropping
uv run corvidia-run --video /usr/share/opencv4/samples/data/vtest.avi --mode realtime --speed 4

# Live CSI camera with Cosmos and preview at http://192.168.2.2:8080/  (Ctrl-C to stop)
uv run corvidia-run --camera --confirmer cosmos --preview-port 8080

# Confirmer accuracy/latency on the labeled crop set, and failure-path checks
uv run corvidia-cosmos-eval --dataset ~/corvidia-data/eval/starter --checks
```

Useful flags: `--model ~/models/yolo11n.pt` (PyTorch path), `--confirmer cosmos|yes|no|uncertain`,
`--stub-delay 0.8`, `--duration 60`, `--config settings.toml`.
Events default to `~/corvidia-data/events/<session>/`, outside the rsync target,
and each session gets a `run_report.json` (detector/loop/frame-age percentiles,
drops, events, skips, peak RSS, peak system memory, max temperatures).

## Step 3 measurements (Orin Nano 8 GB, 2026-09-27)

Starter crop set (`~/corvidia-data/eval/starter`, 255 crops hand-labeled from
vtest.avi, a live session, background regions, and Megamind.avi animation):

| Model, prompt | Person recall | Confirm precision | Background rejected | Cartoons rejected | Latency p50 / p99 |
|---|---|---|---|---|---|
| Q8_0, default prompt | 121/121 | 0.87 | 30/34 | 1/15 | 1.53 / 2.75 s |
| **Q4_K_M, default prompt (deployed)** | 120/121 | 0.95 | 34/34 | 9/15 | 1.35 / 2.46 s |
| Q4_K_M, "physically present" prompt | 41/121 | 1.00 | 34/34 | 15/15 | 1.26 / 2.40 s |

The model never answered "uncertain". Rewording the prompt swings results
sharply, so prompt changes need a larger held-out set with real screens, photos
and mannequins before they are adopted.

Full stack, 4 min real-time replay with Cosmos Q4 (TensorRT detector, no PyTorch):
0 dropped frames at 10 fps; detector p50 22.5 ms idle vs 36.5 ms while Cosmos
runs (GPU contention); confirmation p50 1.51 s, p99 1.77 s, no timeouts; minimum
available RAM 1.73 GB (desktop session running); max 53.8 °C.

Failure paths against the real server: mid-generation cancel acknowledged with
the server idle after ~1.0 s; a 4K image maps to `context_overflow`; a stopped
server maps to `server_unavailable`.

Memory (camera run, 20 s): TensorRT-only pipeline costs 655 MB and runs the
detector at 22.9 ms p50; the Ultralytics/PyTorch path cost 1,051 MB at 52.7 ms.

## Not yet covered

- Spatial/temporal deduplication of fragmented tracks (to be evaluated on footage;
  on `vtest.avi` some people get more than one event after an ID switch).
- Argus sensor timestamps: live frames carry host arrival time only.
- Dataset-collection retention quotas and automatic retention policies.
- Hard negatives (screens, printed photos, mannequins, statues) in the eval set,
  and a held-out split for prompt changes.
- Calibrated `person_score` (the spec keeps it null for now).
- YOLO11n and Ultralytics are AGPL-3.0; a commercial product needs a licence or
  a differently licensed detector.
