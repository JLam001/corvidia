"""Camera/GPU subprocess for the guarded stand demo; deliberately no motor API.

The parent owns serial, deadlines, and motor stop. This process reports advancing
camera frames and durable matching evidence through bounded queues. Importing
this module does not import CUDA, TensorRT, OpenCV, or NumPy into the parent.
"""

from __future__ import annotations

import dataclasses
import math
import queue
import re
import time
import uuid
from pathlib import Path


def capture_mono(frame) -> float:
    info = frame.info
    if info.capture_clock.value == "host_monotonic" and info.capture_ts is not None:
        return float(info.capture_ts)
    return float(info.arrival_mono)


class EventSink:
    """Nonblocking shared IPC writer; overflow permanently stops progress."""

    def __init__(self, output) -> None:
        import threading

        self.output = output
        self.failed = threading.Event()

    def send(self, item: dict) -> bool:
        if self.failed.is_set():
            return False
        try:
            self.output.put_nowait(item)
            return True
        except (queue.Full, BrokenPipeError, EOFError, OSError):
            self.failed.set()
            return False

    def for_mission(self, mission_id: str):
        sink = self

        class MissionObserver:
            def put_nowait(self, item: dict) -> None:
                if not sink.send({**item, "mission_id": mission_id}):
                    raise queue.Full

        return MissionObserver()


class ManualGuidance:
    """Image-based turn cues, not a yaw or position estimate."""

    def __init__(self, started: float) -> None:
        self.started = started
        self.track_id: int | None = None
        self.last_seen = -math.inf

    def update(self, frame, detections, pipeline, now: float) -> str:
        if now - capture_mono(frame) > 0.5:
            return "hold"
        states = {key.track_id: state for key, state in list(pipeline.gate.tracks.items())
                  if key.source_epoch == frame.info.source_epoch}
        if pipeline.worker.state.value in ("active", "draining") or any(
                state.phase.value == "pending" for state in states.values()):
            return "hold"
        eligible = [d for d in detections if d.class_name == "person"
                    and d.confidence >= pipeline.cfg.gate.min_confidence
                    and d.track_id in states
                    and states[d.track_id].phase.value == "observing"]
        selected = next((d for d in eligible if d.track_id == self.track_id), None)
        if selected is None:
            previous = states.get(self.track_id)
            if (self.track_id is not None and now - self.last_seen < 0.5
                    and previous is not None and previous.phase.value == "observing"):
                return "hold"
            selected = max(eligible, key=lambda d: (d.confidence, -d.track_id), default=None)
        if selected is None:
            self.track_id = None
            return "search_right" if int(max(0.0, now - self.started) / 5.0) % 2 == 0 else "search_left"
        self.track_id, self.last_seen = selected.track_id, now
        center = (selected.bbox.x1 + selected.bbox.x2) / (2 * frame.info.width) - 0.5
        return "right" if center > 0.10 else "left" if center < -0.10 else "hold"


def _latest_preview(output, jpeg: bytes) -> None:
    """Preview is disposable. Never drop safety events to make room for pixels."""
    try:
        output.put_nowait(jpeg)
    except queue.Full:
        try:
            output.get_nowait()
        except queue.Empty:
            pass
        try:
            output.put_nowait(jpeg)
        except queue.Full:
            pass


def _begin_values(command: dict) -> tuple[str, str, float, dict]:
    from .cosmos import validate_appearance

    mission_id = command.get("mission_id")
    if not isinstance(mission_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", mission_id):
        raise ValueError("invalid mission_id")
    appearance = validate_appearance(command.get("appearance"))
    started = command.get("start_mono")
    if isinstance(started, bool) or not isinstance(started, (float, int)) or not math.isfinite(started):
        raise ValueError("start_mono must be a finite monotonic time")
    extra = command.get("record_extra", {})
    if not isinstance(extra, dict):
        raise ValueError("record_extra must be an object")
    if "mission" in extra and not isinstance(extra["mission"], dict):
        raise ValueError("record_extra.mission must be an object")
    return mission_id, appearance, float(started), dict(extra)


def _stand_pipeline_config(model: str, root: Path):
    from .config import PipelineConfig

    cfg = PipelineConfig()
    return dataclasses.replace(
        cfg, detector=dataclasses.replace(cfg.detector, model=model),
        storage=dataclasses.replace(cfg.storage, root=root, save_frame=True),
        # The committed primary frame/crop is enough for this demo. Avoid retaining
        # a second full-resolution crop for each tracked person.
        best_shot=dataclasses.replace(cfg.best_shot, enabled=False),
        depth=dataclasses.replace(cfg.depth, enabled=False),
        freespace=dataclasses.replace(cfg.freespace, hz=0),
        confirm=dataclasses.replace(cfg.confirm, deadline_s=4.0),
        # One candidate at a time for understandable HOLD/capture behavior.
        queue=dataclasses.replace(cfg.queue, max_pending=1),
    )


def perception_worker(control_queue, event_queue, preview_queue, configdict: dict) -> None:
    """Multiprocessing spawn target. All GPU imports and resources live here.

    Inputs: begin(mission_id, appearance, record_extra, start_mono), end, shutdown.
    Outputs: ready; progress on every advancing frame; mission-scoped stage,
    completion, skip and fault; begun/ended. Stage has mono, stage and event_id.
    Completion has capture_mono, commit_mono, source_epoch, committed and path.
    Preview queue carries JPEG bytes and should have capacity one.
    """
    sink = EventSink(event_queue)
    source = detector = pipe = backend = None
    mission_id = None
    try:
        # Do not move these imports to module scope: the serial supervisor must
        # remain independent of GPU runtime initialization and contention.
        import cv2

        from .cosmos import LlamaCppBackend, LlamaCppConfig
        from .detector import PersonTracker, make_detector
        from .pipeline import EventPipeline
        from .preview import PreviewState, annotate
        from .sources import CameraSource, argus_pipeline

        model = str(configdict.get("model", "~/models/yolo11n.engine"))
        if not model.endswith(".engine"):
            raise ValueError("stand worker requires the TensorRT YOLO engine")
        width, height, fps = (int(configdict.get(k, v)) for k, v in
                              (("width", 1280), ("height", 720), ("fps", 30)))
        if (width, height) != (1280, 720) or fps not in (15, 30):
            raise ValueError("stand capture supports 1280x720 at 15 or 30 fps")
        root = Path(configdict.get("events_root", "~/corvidia-data/stand-events")).expanduser()
        cfg = _stand_pipeline_config(model, root)
        backend = LlamaCppBackend(LlamaCppConfig(url=configdict.get("cosmos_url", "http://127.0.0.1:8010")))
        if not backend.healthy() or backend.is_idle() is not True:
            raise RuntimeError("real Cosmos server must be healthy and idle")
        detector = make_detector(cfg.detector)
        detector.warmup((height, width, 3))
        tracker = PersonTracker(dataclasses.replace(cfg.tracker, frame_rate=fps))
        source = CameraSource(argus_pipeline(
            int(configdict.get("sensor_id", 0)), int(configdict.get("sensor_mode", 1)),
            output_size=(width, height), fps=fps, flip=int(configdict.get("flip", 0))), fps=fps)
        if not source.connected.wait(15.0):
            raise RuntimeError("CSI camera did not connect within 15 s")
        ready = False
        startup = last_frame = time.monotonic()
        last_identity = None
        last_capture = -math.inf
        mission_epoch = None
        started = None
        guide = None
        last_preview = 0.0
        shutdown = False
        seen_missions: set[str] = set()

        while not shutdown and not sink.failed.is_set():
            # Commands are consumed between advancing camera frames. A wedged
            # GPU cannot delay the parent's independent stop/watchdog.
            for _ in range(32):
                try:
                    command = control_queue.get_nowait()
                except queue.Empty:
                    break
                if not isinstance(command, dict):
                    raise ValueError("perception command must be an object")
                kind = command.get("type")
                if kind in ("end", "shutdown"):
                    ended = mission_id
                    if pipe is not None:
                        pipe.stop()
                        pipe = None
                    mission_id = mission_epoch = started = guide = None
                    tracker.reset()
                    ready = False
                    startup = time.monotonic()
                    if ended is not None:
                        sink.send({"type": "ended", "mission_id": ended, "mono": time.monotonic()})
                    shutdown = kind == "shutdown"
                    if shutdown:
                        break
                elif kind == "begin":
                    if pipe is not None or not ready:
                        raise ValueError("begin requires an idle, ready perception worker")
                    mid, appearance, started, extra = _begin_values(command)
                    if mid in seen_missions:
                        raise ValueError("mission_id cannot be reused")
                    if started > time.monotonic() + 0.05:
                        raise ValueError("mission start time is in the future")
                    seen_missions.add(mid)
                    mission_id = mid
                    mission_epoch = last_identity[0]
                    guide = ManualGuidance(started)
                    tracker.reset()
                    session = "stand-" + uuid.uuid4().hex
                    mission_meta = dict(extra.pop("mission", {}))
                    pipe = EventPipeline(
                        cfg, backend, session_id=session, appearance=appearance,
                        observer_queue=sink.for_mission(mid),
                        record_extra={"mission": {**mission_meta, **extra, "id": mid,
                                                   "appearance": appearance, "start_mono": started,
                                                   "target_class": "person"}},
                    )
                    pipe.start()
                    if not pipe.store.accepting():
                        raise RuntimeError("evidence storage is unavailable")
                    ready = False
                    sink.send({"type": "begun", "mission_id": mid, "session_id": session,
                               "path": str(pipe.store.session_dir.resolve()), "mono": time.monotonic()})
                else:
                    raise ValueError("unknown perception command")
            if shutdown:
                break
            if pipe is not None and pipe.observer_overflow.is_set():
                raise RuntimeError("perception observer queue overflow")
            frame = source.read(timeout=0.05)
            now = time.monotonic()
            if frame is None:
                # Gst PLAYING/connected precedes Argus's first actual buffer.
                # Motors remain inhibited until that buffer has passed YOLO and
                # ready is emitted. This grace never applies after any frame.
                if last_identity is None and now - startup > 15.0:
                    raise RuntimeError("CSI camera produced no first frame within 15 s")
                if last_identity is not None and now - last_frame > 0.75:
                    raise RuntimeError("no advancing camera frame for 0.75 s")
                continue
            identity = (frame.info.source_epoch, frame.info.frame_id)
            captured = capture_mono(frame)
            if (not math.isfinite(captured) or captured > now + 0.05
                    or captured <= last_capture or identity == last_identity):
                raise RuntimeError("camera timestamp or frame identity did not advance")
            if now - captured > 0.5:
                raise RuntimeError("camera frame is stale")
            if last_identity is not None and identity[0] != last_identity[0]:
                tracker.reset()
                if mission_id is not None:
                    raise RuntimeError("camera source epoch changed during mission")
            last_identity, last_capture, last_frame = identity, captured, now
            xyxy, confidence = detector.detect(frame.image)
            detections = tracker.update(xyxy, confidence, frame.info.frame_id)
            processed = time.monotonic()
            if processed - captured > 0.5:
                raise RuntimeError("detector result is stale")
            guidance = "hold"
            if pipe is not None:
                if captured >= started:
                    pipe.on_frame(frame, detections)
                    guidance = guide.update(frame, detections, pipe, processed)
                faults = pipe.health.snapshot()["faults"]
                if faults:
                    raise RuntimeError("perception fault: " + str(faults))
                if pipe.worker.state.value == "unavailable":
                    raise RuntimeError("Cosmos did not recover from a timed-out request")
            elif not ready:
                # A real frame has now traversed the real detector. The backend
                # must be drained after the previous mission before readiness.
                if not backend.healthy() or backend.is_idle() is not True:
                    if processed - startup > 10.0:
                        raise RuntimeError("Cosmos did not become healthy and idle")
                    continue
                ready = True
                sink.send({"type": "ready", "capture_mono": captured,
                           "processed_mono": processed, "source_epoch": identity[0],
                           "model": model, "backend": backend.info.model_revision})
            sink.send({"type": "progress", "mission_id": mission_id,
                       "capture_mono": captured, "processed_mono": processed,
                       "source_epoch": identity[0], "frame_id": identity[1],
                       "capture_quality": frame.info.capture_quality, "guidance": guidance})
            if sink.failed.is_set():
                break
            if preview_queue is not None and processed - last_preview >= 0.2:
                labels = ({key.track_id: state.phase.value for key, state in list(pipe.gate.tracks.items())}
                          if pipe is not None else {})
                preview = annotate(PreviewState(frame, detections, labels, [
                    f"Stand demo: {guidance.replace('_', ' ').upper()}",
                    "Manual turn cues only; stay within stand travel.",
                ]))
                ok, data = cv2.imencode(".jpg", preview, [cv2.IMWRITE_JPEG_QUALITY, 70])
                if ok:
                    _latest_preview(preview_queue, data.tobytes())
                last_preview = processed
    except Exception as exc:  # worker failures must reach the independent supervisor
        sink.send({"type": "fault", "mission_id": mission_id,
                   "reason": f"{type(exc).__name__}: {exc}", "mono": time.monotonic()})
    finally:
        # Stop motors is the parent's job and must precede waiting for this cleanup.
        if pipe is not None:
            try:
                pipe.stop()
            except Exception:
                pass
        if source is not None:
            source.close()
        if detector is not None:
            detector.close()
