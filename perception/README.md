# Perception: person detection event pipeline

Implements [`docs/event-pipeline.md`](../docs/event-pipeline.md):

- **Step 1, event core:** typed records, candidate lifecycle, bounded queues, stub
  confirmer, evidence writer, deterministic detection-event tests.
- **Step 2, video replay:** YOLO11n + explicitly configured ByteTrack, every-frame
  and real-time replay, live CSI camera source, MJPEG preview, run reports.
- Not yet: the Cosmos confirmer (step 3). All runs use a stub confirmer.

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
  detector.py       # YOLO wrapper + ByteTrack with dropped-frame aging
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

PyTorch comes from the `cu130` index. Those wheels have no native sm_87 (Orin)
build; the sm_80 code runs, and `test_gpu_detections_match_cpu` checks the GPU
results against the CPU. The TensorRT FP16 engine is the native-Orin option and
the one to use (vtest.avi, 2026-09-26: 28 ms/frame vs 44-53 ms for `.pt`; 96% of
boxes match at IoU > 0.9). Building it takes about 10 minutes:

```sh
uv run python -c "from ultralytics import YOLO; YOLO('$HOME/models/yolo11n.pt').export(format='engine', half=True, imgsz=640, device=0)"
```

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

# Live CSI camera with preview at http://192.168.2.2:8080/  (Ctrl-C to stop)
uv run corvidia-run --camera --model ~/models/yolo11n.engine --preview-port 8080
```

Useful flags: `--model ~/models/yolo11n.engine`, `--confirmer yes|no|uncertain`,
`--stub-delay 0.8`, `--duration 60`, `--config settings.toml`.
Events default to `~/corvidia-data/events/<session>/`, outside the rsync target,
and each session gets a `run_report.json` (detector/loop/frame-age percentiles,
drops, events, skips, peak RSS, peak system memory, max temperatures).

## Not yet covered

- Spatial/temporal deduplication of fragmented tracks (to be evaluated on footage;
  on `vtest.avi` some people get more than one event after an ID switch).
- Argus sensor timestamps: live frames carry host arrival time only.
- Dataset-collection retention quotas and automatic retention policies.
- A real Cosmos adapter (step 3). A new backend implements `ConfirmerBackend`:
  `submit()` without transport retries, `cancel()` returning True only when the
  server confirms it is idle, and `reset()` for controlled recovery.
