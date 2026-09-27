"""Camera/video -> YOLO11n + ByteTrack -> event pipeline, with preview and a run report.

Examples (on the Jetson, from ~/corvidia/perception):

    # Accuracy replay: every frame, event logic on video time, instant stub confirmer
    # (with --confirmer cosmos, each confirmation is awaited before the next frame)
    uv run corvidia-run --video /usr/share/opencv4/samples/data/vtest.avi --mode every

    # Timing replay: real-time pacing (optionally sped up) with frame dropping
    uv run corvidia-run --video vtest.avi --mode realtime --speed 3

    # Live CSI camera with Cosmos and browser preview at http://192.168.2.2:8080/
    uv run corvidia-run --camera --confirmer cosmos --preview-port 8080
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import resource
import signal
import sys
import threading
import time
from collections import Counter, deque
from pathlib import Path

import numpy as np

from .config import PipelineConfig, load_config
from .confirmer import DelayedStubBackend, StubBackend
from .cosmos import LlamaCppBackend, LlamaCppConfig
from .worker import WorkerState
from .detector import PersonTracker, make_detector
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

    def __init__(self, interval_s: float = 2.0, health=None, min_available_mb: int = 0) -> None:
        self._health = health
        self._min_available_mb = min_available_mb
        self.min_available_kb: int | None = None
        self.mem_total_kb: int | None = None
        self.max_temp_c: dict[str, float] = {}
        self.current_temp_c: dict[str, float] = {}
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
            if self._health is not None:
                self._health.set("mem_available_mb", avail // 1024)
                if avail // 1024 < self._min_available_mb:
                    self._health.fault("memory_low", f"{avail // 1024} MB available, "
                                       f"floor {self._min_available_mb} MB")
                else:
                    self._health.clear_fault("memory_low")
        except (OSError, KeyError, ValueError):
            pass
        for zone in Path("/sys/class/thermal").glob("thermal_zone*"):
            try:
                name = (zone / "type").read_text().strip()
                temp = int((zone / "temp").read_text()) / 1000
            except (OSError, ValueError):
                continue
            self.current_temp_c[name] = temp
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
        min_avail = self.min_available_kb // 1024 if self.min_available_kb is not None else None
        return {"peak_system_used_mb": peak_used, "min_available_mb": min_avail,
                "max_temp_c": self.max_temp_c}


def power_mode() -> str | None:
    """Jetson nvpmodel mode name, e.g. "15W" (None when unavailable)."""
    import subprocess

    try:
        out = subprocess.run(["nvpmodel", "-q"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if line.startswith("NV Power Mode:"):
            return line.split(":", 1)[1].strip()
    return None


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
    ap.add_argument("--sensor-mode", type=int, choices=[0, 1], default=1,
                    help="IMX477 mode: 0 = 3840x2160@30, 1 = 1920x1080@60 (binned)")
    ap.add_argument("--camera-size", default="1280x720",
                    help="camera output size WxH (720p leaves RAM for depth; 1920x1080 for sharper crops)")
    ap.add_argument("--camera-fps", type=int, default=30)
    ap.add_argument("--flip", type=int, default=0, help="nvvidconv flip-method (0-7)")
    ap.add_argument("--config", type=Path, help="TOML config (sections mirror PipelineConfig)")
    ap.add_argument("--model", help="override detector model (.pt or .engine)")
    ap.add_argument("--no-depth", action="store_true", help="skip per-event depth estimation")
    ap.add_argument("--depth-model", help="override depth engine (e.g. the outdoor model)")
    ap.add_argument("--freespace-hz", type=float, default=None,
                    help="publish an obstacle free-space profile from the depth engine at this rate "
                         "on its own thread (0 = off; needs the depth engine; 10 is the Nano target)")
    ap.add_argument("--freespace-udp", default=None,
                    help="also send each free-space profile as a JSON datagram to host:port")
    ap.add_argument("--tracks-udp", default=None,
                    help="send per-frame person tracks (normalized boxes, id, conf, age, depth_m) as JSON "
                         "datagrams to host:port, for an external mission loop")
    ap.add_argument("--events", type=Path, help="override storage root")
    ap.add_argument("--confirmer", choices=["cosmos", *sorted(STUB_ANSWERS)], default="yes",
                    help="'cosmos' uses the local llama-server; otherwise a stub answer")
    ap.add_argument("--cosmos-url", default=LlamaCppConfig.url)
    ap.add_argument("--stub-delay", type=float, default=0.8,
                    help="stub inference delay in realtime modes, seconds")
    ap.add_argument("--preview-port", type=int, default=None, help="serve MJPEG preview on this port")
    ap.add_argument("--max-frames", type=int, default=None)
    ap.add_argument("--duration", type=float, default=None, help="stop after this many seconds")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--timeseries-interval", type=float, default=10.0,
                    help="seconds between timeseries.jsonl rows in the session directory (0: off)")
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
    detector = make_detector(cfg.detector)
    if args.camera:
        w, h = (int(x) for x in args.camera_size.lower().split("x"))
        detector.warmup((h, w, 3))

    # -- source -----------------------------------------------------------------------
    if args.camera:
        source = CameraSource(
            argus_pipeline(args.sensor_id, args.sensor_mode, output_size=(w, h),
                           fps=args.camera_fps, flip=args.flip),
            source_id=f"csi:{args.sensor_id}", fps=args.camera_fps)
        if not source.connected.wait(15.0):
            source.close()
            raise SystemExit("camera did not start within 15 s (is nvargus-daemon running?)")
    else:
        source = VideoFileSource(args.video, realtime=realtime, speed=args.speed, loop=args.loop)

    # -- tracker, pipeline -------------------------------------------------------------
    tracker = PersonTracker(dataclasses.replace(cfg.tracker, frame_rate=round(source.fps)))
    if args.confirmer == "cosmos":
        backend = LlamaCppBackend(LlamaCppConfig(url=args.cosmos_url))
        if not backend.healthy():
            source.close()
            raise SystemExit(f"llama-server not healthy at {args.cosmos_url} "
                             "(sudo systemctl status cosmos-server)")
    elif realtime:
        answer = STUB_ANSWERS[args.confirmer]
        backend = DelayedStubBackend(args.stub_delay, lambda _r: answer)
    else:
        answer = STUB_ANSWERS[args.confirmer]
        backend = StubBackend(lambda _r: answer)
    clock = time.monotonic if realtime else ReplayClock()
    extra = {"detector": {"model": detector.model_path, "imgsz": cfg.detector.imgsz,
                          "tracker": "bytetrack", "tracker_config": dataclasses.asdict(cfg.tracker)}}
    if args.no_depth or args.depth_model:
        cfg = dataclasses.replace(cfg, depth=dataclasses.replace(
            cfg.depth, enabled=not args.no_depth, model=args.depth_model or cfg.depth.model))
    depth = None
    if cfg.depth.enabled:
        from .depth import DepthEstimator

        if not Path(cfg.depth.model).expanduser().exists():
            source.close()
            raise SystemExit(f"depth engine {cfg.depth.model} not found (build it, or pass --no-depth)")
        depth = DepthEstimator(cfg.depth)
        extra["depth_model"] = depth.model_path
    if args.freespace_hz is not None or args.freespace_udp is not None:
        cfg = dataclasses.replace(cfg, freespace=dataclasses.replace(
            cfg.freespace,
            hz=cfg.freespace.hz if args.freespace_hz is None else args.freespace_hz,
            udp=cfg.freespace.udp if args.freespace_udp is None else args.freespace_udp))
    if args.tracks_udp is not None:
        cfg = dataclasses.replace(cfg, tracks=dataclasses.replace(cfg.tracks, udp=args.tracks_udp))
    if cfg.freespace.udp and cfg.freespace.hz <= 0:
        source.close()
        raise SystemExit("--freespace-udp needs a rate: pass --freespace-hz (10 is the Nano target)")
    if cfg.freespace.hz > 0 and depth is None:
        source.close()
        raise SystemExit("the free-space stream needs the depth engine (drop --no-depth)")
    pipe = EventPipeline(cfg, backend, clock=clock, record_extra=extra, depth=depth)
    if realtime:
        pipe.start()
    else:
        pipe.open()
    freespace = None
    if cfg.freespace.hz > 0:
        from .depth_stream import FreeSpaceStream

        freespace = FreeSpaceStream(
            depth, cfg.freespace, pipe.health,
            log_path=pipe.store.session_dir / "freespace.jsonl" if pipe.store is not None else None,
            scale=cfg.depth.scale)
    tracks_pub = None
    if cfg.tracks.udp:
        from .tracks_stream import TracksPublisher

        tracks_pub = TracksPublisher(cfg.tracks, pipe.health,
                                     depth_map_source=freespace.latest_map if freespace is not None else None)

    det_ms: deque[float] = deque(maxlen=200_000)
    # Detector time split by whether a confirmation was in flight (GPU contention).
    det_ms_confirming: deque[float] = deque(maxlen=200_000)
    det_ms_idle: deque[float] = deque(maxlen=200_000)
    loop_ms: deque[float] = deque(maxlen=200_000)
    age_ms: deque[float] = deque(maxlen=200_000)
    capture_age_ms: deque[float] = deque(maxlen=200_000)
    dropped = 0
    processed = 0
    last_id: dict[int, int] = {}
    sampler = SystemSampler(health=pipe.health, min_available_mb=cfg.system.min_available_mb)
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
    if freespace is not None:
        freespace.start()
    started = time.monotonic()
    last_print = started
    last_series = started
    series_path = (pipe.store.session_dir / "timeseries.jsonl"
                   if pipe.store is not None and args.timeseries_interval > 0 else None)
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
            if freespace is not None:
                freespace.offer(frame)  # fan-out: the depth thread always sees the newest frame
            age_ms.append((t0 - frame.info.arrival_mono) * 1000)
            if frame.info.capture_quality == "argus_buffer_pts" and frame.info.capture_ts is not None:
                capture_age_ms.append((t0 - frame.info.capture_ts) * 1000)

            xyxy, conf = detector.detect(frame.image)
            dets = tracker.update(xyxy, conf, frame.info.frame_id)
            if isinstance(clock, ReplayClock):
                clock.set(frame)
            pipe.on_frame(frame, dets)
            if tracks_pub is not None:
                tracks_pub.publish(frame, dets)
            if not realtime:
                pipe.tick()
                # Accuracy replay waits for each confirmation (video time is frozen,
                # so the deadline cannot fire; the HTTP timeout bounds the wait).
                while pipe.worker.state is WorkerState.ACTIVE and not stop.is_set():
                    time.sleep(0.005)
                    pipe.tick()
            processed += 1
            det_ms.append(detector.last_ms)
            (det_ms_confirming if pipe.worker.state is WorkerState.ACTIVE
             else det_ms_idle).append(detector.last_ms)
            loop_ms.append((time.monotonic() - t0) * 1000)
            pipe.health.set("detector_ms", round(detector.last_ms, 1))
            pipe.health.set("processed", processed)
            pipe.health.set("dropped", dropped)

            if preview is not None:
                preview.update(PreviewState(frame, dets, _labels(pipe, epoch), _stats(
                    source, pipe, det_ms, loop_ms, age_ms, processed, dropped)))
            if series_path is not None and time.monotonic() - last_series >= args.timeseries_interval:
                last_series = time.monotonic()
                _append_series(series_path, last_series - started, pipe, sampler, det_ms, loop_ms,
                               processed, dropped)
            if not args.quiet and time.monotonic() - last_print >= 5.0:
                last_print = time.monotonic()
                print(" | ".join(_stats(source, pipe, det_ms, loop_ms, age_ms, processed, dropped)))
    finally:
        signal.signal(signal.SIGINT, prev_handler)
        elapsed = time.monotonic() - started
        if freespace is not None:
            freespace.stop()
        if tracks_pub is not None:
            tracks_pub.close()
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
    report["detector_ms_while_confirming"] = pct(det_ms_confirming)
    # Driver timestamp -> detector start: includes conversion and queueing.
    report["capture_to_detect_ms"] = pct(capture_age_ms)
    if args.camera:
        report["camera"] = {"sensor_id": args.sensor_id, "sensor_mode": args.sensor_mode,
                            "output_size": args.camera_size, "fps": args.camera_fps,
                            "flip": args.flip}
    report["power_mode"] = power_mode()
    if depth is not None:
        dists = [e.get("distance_m") for e in _events(pipe) if e.get("distance_m") is not None]
        report["depth"] = {"model": depth.model_path, "scale": cfg.depth.scale,
                           "calibrated": cfg.depth.calibrated, "events_with_distance": len(dists),
                           "distance_m": pct(dists), "depth_ms_last": depth.last_ms}
    report["detector_ms_while_idle"] = pct(det_ms_idle)
    if freespace is not None:
        report["freespace"] = freespace.summary()
    if tracks_pub is not None:
        report["tracks_stream"] = tracks_pub.summary()
    if pipe.store is not None:
        (pipe.store.session_dir / "run_report.json").write_text(json.dumps(report, indent=2, default=str))
    if not args.quiet:
        print(json.dumps(report, indent=2, default=str))
    return report


def _append_series(path: Path, t: float, pipe: EventPipeline, sampler: SystemSampler,
                   det_ms, loop_ms, processed: int, dropped: int) -> None:
    """One row of drift data for soak tests (window = last 300 frames)."""
    snap = pipe.health.snapshot()
    recent_det = list(det_ms)[-300:]
    recent_loop = list(loop_ms)[-300:]
    row = {
        "t_s": round(t, 1),
        "processed": processed,
        "dropped": dropped,
        "loop_fps": round(1000 / float(np.mean(recent_loop)), 1) if recent_loop else None,
        "detector_ms_p50": round(float(np.median(recent_det)), 1) if recent_det else None,
        "detector_ms_p95": round(float(np.percentile(recent_det, 95)), 1) if recent_det else None,
        "mem_available_mb": snap["gauges"].get("mem_available_mb"),
        "temps_c": dict(sampler.current_temp_c),
        "queue_depth": snap["gauges"].get("queue_depth"),
        "worker_state": snap["gauges"].get("worker_state"),
        "last_inference_s": snap["gauges"].get("last_inference_s"),
        "freespace": {k[10:]: v for k, v in snap["gauges"].items() if k.startswith("freespace_")},
        "counters": snap["counters"],
        "faults": snap["faults"],
    }
    try:
        with open(path, "a") as f:
            f.write(json.dumps(row) + "\n")
    except OSError:
        pipe.health.incr("timeseries_write_errors")


def _events(pipe: EventPipeline) -> list[dict]:
    if pipe.store is None:
        return []
    return [json.loads(p.read_text()) for p in sorted(pipe.store.session_dir.glob("*/event.json"))]


def _labels(pipe: EventPipeline, epoch: int | None) -> dict[int, str]:
    labels: dict[int, str] = {}
    for key, st in list(pipe.gate.tracks.items()):
        if key.source_epoch == epoch:
            labels[key.track_id] = st.phase.value
    for c in list(pipe.completions):
        if c.key.source_epoch == epoch and labels.get(c.key.track_id) in (
                Phase.SUPPRESSED.value, Phase.COOLDOWN.value):
            labels[c.key.track_id] = c.result.value + (
                f" {c.distance_m:.1f}m" if c.distance_m is not None else "")
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
    g = snap["gauges"]
    if g.get("freespace_hz") is not None or "freespace_min_m" in g:
        lines.append(f"free space {g.get('freespace_hz') or 0:.1f} Hz  nearest {g.get('freespace_min_m', '-')} m"
                     f"  center {g.get('freespace_center_free', '-')}  map age {g.get('freespace_age_ms', '-')} ms"
                     f"{'  ALL CLOSE' if g.get('freespace_all_close') else ''}")
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
    inference = [e.get("inference_wall_s", e.get("inference_duration_s")) * 1000 for e in events
                 if e.get("inference_wall_s", e.get("inference_duration_s"))]
    queue_delay = [e["queue_delay_s"] * 1000 for e in events if e.get("queue_delay_s") is not None]
    tracks_per_epoch = Counter((e["source_epoch"], e["track_id"]) for e in events)
    skips = Counter()
    if pipe.store is not None and (pipe.store.session_dir / "skips.jsonl").exists():
        for line in (pipe.store.session_dir / "skips.jsonl").read_text().splitlines():
            skips[json.loads(line)["reason"]] += 1
    gpu_peak = None
    torch = sys.modules.get("torch")  # only when a .pt model loaded it; never import it here
    if torch is not None and torch.cuda.is_available():
        gpu_peak = torch.cuda.max_memory_allocated() / 2**20
    return {
        "session_id": pipe.session_id,
        "source": source.source_id,
        "mode": "camera" if args.camera else (args.mode or "every"),
        "speed": args.speed,
        "model": detector.model_path,
        "detector_runtime": type(detector).__name__,
        "torch_loaded": "torch" in sys.modules,
        "confirmer": ("cosmos:" + pipe.worker.backend_info.model_revision
                      if args.confirmer == "cosmos" else f"stub:{args.confirmer}"),
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
