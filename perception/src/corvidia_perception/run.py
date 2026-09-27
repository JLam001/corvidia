"""Camera/video -> YOLO11n + ByteTrack -> event pipeline, with preview and a run report.

Examples (on the Jetson, from ~/corvidia/perception):

    # Accuracy replay: every frame, event logic on video time, instant stub confirmer
    uv run corvidia-run --video /usr/share/opencv4/samples/data/vtest.avi --mode every

    # Timing replay: real-time pacing (optionally sped up) with frame dropping
    uv run corvidia-run --video vtest.avi --mode realtime --speed 3

    # Live CSI camera with browser preview at http://192.168.2.2:8080/
    uv run corvidia-run --camera --preview-port 8080
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import resource
import signal
import threading
import time
from collections import Counter, deque
from pathlib import Path

import numpy as np

from .config import PipelineConfig, load_config
from .confirmer import DelayedStubBackend, StubBackend
from .detector import PersonTracker, YoloDetector
from .gate import Phase
from .pipeline import EventPipeline
from .preview import PreviewServer, PreviewState
from .records import Frame
from .sources import CameraSource, VideoFileSource, argus_pipeline

STUB_ANSWERS = {"yes": '{"answer": "yes"}', "no": '{"answer": "no"}',
                "uncertain": '{"answer": "uncertain"}'}


class ReplayClock:
    """Video presentation time as the pipeline clock, monotonic across loop epochs."""

    def __init__(self) -> None:
        self.t = 0.0
        self._offset = 0.0
        self._epoch = 0
        self._last = 0.0

    def set(self, frame: Frame) -> None:
        if frame.info.source_epoch != self._epoch:
            self._offset = self.t + 1.0
            self._epoch = frame.info.source_epoch
        self.t = self._offset + (frame.info.pts_s or 0.0)

    def __call__(self) -> float:
        return self.t


class SystemSampler:
    """Peak system memory and temperatures, sampled on a background thread."""

    def __init__(self, interval_s: float = 2.0) -> None:
        self.min_available_kb: int | None = None
        self.mem_total_kb: int | None = None
        self.max_temp_c: dict[str, float] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(interval_s,), daemon=True)
        self._thread.start()

    def sample(self) -> None:
        try:
            info = {}
            for line in Path("/proc/meminfo").read_text().splitlines():
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0])
            self.mem_total_kb = info["MemTotal"]
            avail = info["MemAvailable"]
            if self.min_available_kb is None or avail < self.min_available_kb:
                self.min_available_kb = avail
        except (OSError, KeyError, ValueError):
            pass
        for zone in Path("/sys/class/thermal").glob("thermal_zone*"):
            try:
                name = (zone / "type").read_text().strip()
                temp = int((zone / "temp").read_text()) / 1000
            except (OSError, ValueError):
                continue
            self.max_temp_c[name] = max(temp, self.max_temp_c.get(name, -273.0))

    def _run(self, interval_s: float) -> None:
        while not self._stop.is_set():
            self.sample()
            self._stop.wait(interval_s)

    def stop(self) -> dict:
        self._stop.set()
        self.sample()
        peak_used = None
        if self.mem_total_kb and self.min_available_kb is not None:
            peak_used = (self.mem_total_kb - self.min_available_kb) / 1024
        return {"peak_system_used_mb": peak_used, "max_temp_c": self.max_temp_c}


def pct(values, qs=(50, 95, 99)) -> dict[str, float | None]:
    if not values:
        return {f"p{q}": None for q in qs}
    arr = np.asarray(values, dtype=float)
    return {f"p{q}": round(float(np.percentile(arr, q)), 2) for q in qs}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--video", type=Path, help="recorded video file")
    src.add_argument("--camera", action="store_true", help="live CSI camera via Argus")
    ap.add_argument("--mode", choices=["every", "realtime"], default=None,
                    help="video replay mode (default: every); the camera is always realtime")
    ap.add_argument("--speed", type=float, default=1.0, help="realtime replay speed multiplier")
    ap.add_argument("--loop", action="store_true", help="loop the video (each loop is a new epoch)")
    ap.add_argument("--sensor-id", type=int, default=0)
    ap.add_argument("--camera-size", default="1280x720", help="camera output size WxH")
    ap.add_argument("--camera-fps", type=int, default=30)
    ap.add_argument("--flip", type=int, default=0, help="nvvidconv flip-method (0-7)")
    ap.add_argument("--config", type=Path, help="TOML config (sections mirror PipelineConfig)")
    ap.add_argument("--model", help="override detector model (.pt or .engine)")
    ap.add_argument("--events", type=Path, help="override storage root")
    ap.add_argument("--confirmer", choices=sorted(STUB_ANSWERS), default="yes",
                    help="stub confirmer answer (Cosmos is step 3)")
    ap.add_argument("--stub-delay", type=float, default=0.8,
                    help="stub inference delay in realtime modes, seconds")
    ap.add_argument("--preview-port", type=int, default=None, help="serve MJPEG preview on this port")
    ap.add_argument("--max-frames", type=int, default=None)
    ap.add_argument("--duration", type=float, default=None, help="stop after this many seconds")
    ap.add_argument("--quiet", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> None:
    run(argv)


def run(argv: list[str] | None = None) -> dict:
    """Run the pipeline and return the run report."""
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config) if args.config else PipelineConfig()
    if args.model:
        cfg = dataclasses.replace(cfg, detector=dataclasses.replace(cfg.detector, model=args.model))
    if args.events:
        cfg = dataclasses.replace(cfg, storage=dataclasses.replace(cfg.storage, root=args.events))
    realtime = args.camera or args.mode == "realtime"

    # Load and warm up the detector first so a live camera does not queue frames
    # during CUDA initialization.
    detector = YoloDetector(cfg.detector)
    if args.camera:
        w, h = (int(x) for x in args.camera_size.lower().split("x"))
        detector.warmup((h, w, 3))

    # -- source -----------------------------------------------------------------------
    if args.camera:
        source = CameraSource(
            argus_pipeline(args.sensor_id, output_size=(w, h), fps=args.camera_fps, flip=args.flip),
            source_id=f"csi:{args.sensor_id}", fps=args.camera_fps)
        if not source.connected.wait(15.0):
            source.close()
            raise SystemExit("camera did not start within 15 s (is nvargus-daemon running?)")
    else:
        source = VideoFileSource(args.video, realtime=realtime, speed=args.speed, loop=args.loop)

    # -- tracker, pipeline -------------------------------------------------------------
    tracker = PersonTracker(dataclasses.replace(cfg.tracker, frame_rate=round(source.fps)))
    answer = STUB_ANSWERS[args.confirmer]
    if realtime:
        backend = DelayedStubBackend(args.stub_delay, lambda _r: answer)
        clock = time.monotonic
    else:
        backend = StubBackend(lambda _r: answer)
        clock = ReplayClock()
    extra = {"detector": {"model": detector.model_path, "imgsz": cfg.detector.imgsz,
                          "tracker": "bytetrack", "tracker_config": dataclasses.asdict(cfg.tracker)}}
    pipe = EventPipeline(cfg, backend, clock=clock, record_extra=extra)
    if realtime:
        pipe.start()
    else:
        pipe.open()

    det_ms: deque[float] = deque(maxlen=200_000)
    loop_ms: deque[float] = deque(maxlen=200_000)
    age_ms: deque[float] = deque(maxlen=200_000)
    dropped = 0
    processed = 0
    last_id: dict[int, int] = {}
    sampler = SystemSampler()
    preview = None
    if args.preview_port is not None:
        preview = PreviewServer(args.preview_port, health_fn=lambda: health_view())

    def health_view() -> dict:
        snap = pipe.health.snapshot()
        snap["source"] = source.health.snapshot()
        snap["processed"] = processed
        snap["dropped"] = dropped
        return snap

    stop = threading.Event()
    prev_handler = signal.signal(signal.SIGINT, lambda *_: stop.set())
    if not args.quiet:
        print(f"session {pipe.session_id} -> {pipe.store.session_dir if pipe.store else '-'}")
        if preview:
            print(f"preview: http://<jetson-ip>:{preview.port}/")

    if not args.camera:
        detector.warmup((source.height, source.width, 3))
    started = time.monotonic()
    last_print = started
    source_timed_out = False
    epoch = None
    try:
        while not stop.is_set():
            if args.max_frames is not None and processed >= args.max_frames:
                break
            if args.duration is not None and time.monotonic() - started >= args.duration:
                break
            frame = source.read(timeout=1.0)
            if frame is None:
                if source.finished:
                    break
                if not source_timed_out:
                    source_timed_out = True
                    pipe.source_lost()
                    pipe.health.fault("source", "no frame for 1 s")
                continue
            if source_timed_out:
                source_timed_out = False
                pipe.health.clear_fault("source")
            t0 = time.monotonic()
            if frame.info.source_epoch != epoch:
                tracker.reset()
                epoch = frame.info.source_epoch
                last_id.pop(epoch, None)
            prev = last_id.get(epoch)
            if prev is not None and frame.info.frame_id > prev + 1:
                dropped += frame.info.frame_id - prev - 1
            last_id[epoch] = frame.info.frame_id
            age_ms.append((t0 - frame.info.arrival_mono) * 1000)

            xyxy, conf = detector.detect(frame.image)
            dets = tracker.update(xyxy, conf, frame.info.frame_id)
            if isinstance(clock, ReplayClock):
                clock.set(frame)
            pipe.on_frame(frame, dets)
            if not realtime:
                pipe.tick()
            processed += 1
            det_ms.append(detector.last_ms)
            loop_ms.append((time.monotonic() - t0) * 1000)
            pipe.health.set("detector_ms", round(detector.last_ms, 1))
            pipe.health.set("processed", processed)
            pipe.health.set("dropped", dropped)

            if preview is not None:
                preview.update(PreviewState(frame, dets, _labels(pipe, epoch), _stats(
                    source, pipe, det_ms, loop_ms, age_ms, processed, dropped)))
            if not args.quiet and time.monotonic() - last_print >= 5.0:
                last_print = time.monotonic()
                print(" | ".join(_stats(source, pipe, det_ms, loop_ms, age_ms, processed, dropped)))
    finally:
        signal.signal(signal.SIGINT, prev_handler)
        elapsed = time.monotonic() - started
        if realtime:
            # Let an in-flight stub answer land before stopping.
            deadline = time.monotonic() + cfg.confirm.deadline_s
            while pipe.worker.state.value in ("active", "draining") and time.monotonic() < deadline:
                time.sleep(0.05)
        pipe.stop()
        source.close()
        if preview is not None:
            preview.close()
        system = sampler.stop()

    report = _report(args, cfg, pipe, source, detector, processed, dropped, elapsed,
                     det_ms, loop_ms, age_ms, system)
    if pipe.store is not None:
        (pipe.store.session_dir / "run_report.json").write_text(json.dumps(report, indent=2, default=str))
    if not args.quiet:
        print(json.dumps(report, indent=2, default=str))
    return report


def _labels(pipe: EventPipeline, epoch: int | None) -> dict[int, str]:
    labels: dict[int, str] = {}
    for key, st in list(pipe.gate.tracks.items()):
        if key.source_epoch == epoch:
            labels[key.track_id] = st.phase.value
    for c in list(pipe.completions):
        if c.key.source_epoch == epoch and labels.get(c.key.track_id) in (
                Phase.SUPPRESSED.value, Phase.COOLDOWN.value):
            labels[c.key.track_id] = c.result.value
    return labels


def _stats(source, pipe, det_ms, loop_ms, age_ms, processed, dropped) -> list[str]:
    recent = list(loop_ms)[-30:]
    fps = 1000 / np.mean(recent) if recent else 0.0
    snap = pipe.health.snapshot()
    counters = snap["counters"]
    results = ", ".join(f"{k[7:]}={v}" for k, v in sorted(counters.items()) if k.startswith("result_"))
    lines = [
        f"{source.source_id} epoch {getattr(source, 'epoch', 0)}  processed {processed}  dropped {dropped}",
        f"det {det_ms[-1] if det_ms else 0:.1f} ms  loop {fps:.1f} fps  frame age "
        f"{age_ms[-1] if age_ms else 0:.0f} ms",
        f"queue {snap['gauges'].get('queue_depth', 0)}  worker {snap['gauges'].get('worker_state', '-')}"
        f"  {results or 'no results yet'}",
    ]
    if snap["faults"]:
        lines.append("FAULTS: " + ", ".join(snap["faults"]))
    return lines


def _report(args, cfg, pipe, source, detector, processed, dropped, elapsed, det_ms, loop_ms,
            age_ms, system) -> dict:
    events = []
    if pipe.store is not None:
        for p in sorted(pipe.store.session_dir.glob("*/event.json")):
            events.append(json.loads(p.read_text()))
    results = Counter(e.get("result") or e.get("status") for e in events)
    inference = [e["inference_duration_s"] * 1000 for e in events if e.get("inference_duration_s")]
    queue_delay = [e["queue_delay_s"] * 1000 for e in events if e.get("queue_delay_s") is not None]
    tracks_per_epoch = Counter((e["source_epoch"], e["track_id"]) for e in events)
    skips = Counter()
    if pipe.store is not None and (pipe.store.session_dir / "skips.jsonl").exists():
        for line in (pipe.store.session_dir / "skips.jsonl").read_text().splitlines():
            skips[json.loads(line)["reason"]] += 1
    try:
        import torch

        gpu_peak = torch.cuda.max_memory_allocated() / 2**20 if torch.cuda.is_available() else None
    except Exception:  # noqa: BLE001
        gpu_peak = None
    return {
        "session_id": pipe.session_id,
        "source": source.source_id,
        "mode": "camera" if args.camera else (args.mode or "every"),
        "speed": args.speed,
        "model": detector.model_path,
        "confirmer": f"stub:{args.confirmer}",
        "elapsed_s": round(elapsed, 2),
        "frames_processed": processed,
        "frames_dropped": dropped,
        "processed_fps": round(processed / elapsed, 2) if elapsed else None,
        "detector_ms": pct(det_ms),
        "loop_ms": pct(loop_ms),
        "frame_age_ms": pct(age_ms),
        "events": len(events),
        "results": dict(results),
        "max_events_per_track": max(tracks_per_epoch.values(), default=0),
        "skips": dict(skips),
        "confirmation_ms": pct(inference),
        "queue_delay_ms": pct(queue_delay),
        "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
        "gpu_peak_allocated_mb": round(gpu_peak, 1) if gpu_peak is not None else None,
        **system,
        "health": pipe.health.snapshot(),
        "source_health": source.health.snapshot(),
    }


if __name__ == "__main__":
    main()
