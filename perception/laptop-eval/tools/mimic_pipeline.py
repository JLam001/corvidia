"""Laptop run of the perception pipeline order: the packaged free-space stream
(corvidia_perception.depth_stream.FreeSpaceStream) on its own thread at a target rate, YOLO
person tracking on the detector thread, the gate + evidence store with per-event distance, and
no Cosmos in the loop. On the Jetson the same thing is `corvidia-run --camera --freespace-hz 10`.

    mimic_pipeline.py <clip dir> --events runs/<clip> [--depth-hz 10] [--mode realtime|every]
        [--speed 1.0] [--stale-s 0.15] [--control-hz 20] [--grace-s 0.5]

Laptop stand-ins (the structure and the stream code are the real ones, the speeds are not):
    detector   YOLO11n via Ultralytics on CPU, same weights the Jetson engine was exported from
    depth      Depth Anything V2 Metric Indoor Small via transformers on CPU, resized to the
               TensorRT engine's 518x294 input exactly as corvidia_perception.depth does
Outputs: the normal session (events, best shots, run_report.json, freespace.jsonl) plus
mimic_report.json: rates, latencies, and a virtual controller replayed against freespace.jsonl
to check the consumer's freshness rule (the autodrone layers use 150 ms) under both stamping
choices. `--speed 0.25` with every time constant x4 (--stale-s 0.6 --control-hz 5 --grace-s 2
--depth-hz 2.5) emulates compute 4x faster than the laptop; the gate/queue windows are scaled too.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import threading
import time
from collections import deque
from pathlib import Path

import cv2
import numpy as np

from corvidia_perception.config import DepthConfig, PipelineConfig, load_config
from corvidia_perception.confirmer import StubBackend
from corvidia_perception.depth import MEAN, STD, DepthEstimator
from corvidia_perception.depth_stream import FreeSpaceStream
from corvidia_perception.tracks_stream import TracksPublisher
from corvidia_perception.detector import PersonTracker, make_detector
from corvidia_perception.pipeline import EventPipeline
from corvidia_perception.run import ReplayClock, pct
from corvidia_perception.sources import VideoFileSource
from corvidia_perception.worker import WorkerState

ROOT = Path(__file__).resolve().parents[1]


class CpuDepth(DepthEstimator):
    """Same interface and preprocessing as the TensorRT DepthEstimator, PyTorch CPU underneath."""

    def __init__(self, checkpoint: str, scale: float = 1.0, height: int = 294, width: int = 518) -> None:
        import torch
        from transformers import AutoModelForDepthEstimation

        self.cfg = DepthConfig(model=checkpoint, scale=scale, calibrated=False)
        self.model_path = checkpoint
        self.height, self.width = height, width
        self.meta = json.loads((Path(checkpoint) / "config.json").read_text())
        self.last_ms = 0.0
        self.last_wait_ms = 0.0
        self.wait_ms: list[float] = []
        self._torch = torch
        self._model = AutoModelForDepthEstimation.from_pretrained(checkpoint).eval()
        self._lock = threading.Lock()  # depth thread and confirmation worker share one model

    def depth_map(self, bgr: np.ndarray) -> np.ndarray:
        small = cv2.resize(bgr, (self.width, self.height), interpolation=cv2.INTER_AREA)
        rgb = small[:, :, ::-1].astype(np.float32) * (1 / 255.0)
        inp = self._torch.from_numpy(((rgb - MEAN) / STD).transpose(2, 0, 1).copy())[None]
        t_wait = time.perf_counter()
        with self._lock:
            t = time.perf_counter()
            with self._torch.inference_mode():
                out = self._model(pixel_values=inp).predicted_depth[0].numpy().astype(np.float32)
            self.last_ms = (time.perf_counter() - t) * 1000      # inference only (the TRT number)
            self.last_wait_ms = (t - t_wait) * 1000               # time blocked behind the other thread
            self.wait_ms.append(self.last_wait_ms)
        return out

    def close(self) -> None:
        pass


class PacedSource(VideoFileSource):
    """VideoFileSource stamps arrival_mono at decode time and then sleeps until the frame's pts;
    re-stamp at put time so ages measure the slot, not the pacing sleep."""

    def _run(self) -> None:
        from corvidia_perception.records import Frame
        start = time.monotonic()
        epoch = self.epoch
        while not self._stop.is_set():
            frame = self._decode()
            if frame is None:
                self._eof = True
                self._slot.put_end()
                return
            if frame.info.source_epoch != epoch:
                epoch, start = frame.info.source_epoch, time.monotonic()
            delay = start + (frame.info.pts_s or 0.0) / self.speed - time.monotonic()
            if delay > 0:
                self._stop.wait(delay)
            elif delay < -0.25:
                start -= delay
            info = dataclasses.replace(frame.info, arrival_mono=time.monotonic(), arrival_wall=time.time())
            self._slot.put(Frame(info, frame.image))


def controller_freshness(rows: list[dict], t0: float, t1: float, control_hz: float, stale_s: float,
                         grace_s: float = 0.5) -> dict:
    """Replay a virtual controller against the publish log: what fraction of its ticks saw
    free space younger than stale_s, under the two stamping choices, and how many times the
    L4 guard (stale continuously for grace_s) would have fired."""
    if not rows:
        return {"ticks": 0}
    pubs = np.array([r["t_pub_mono"] for r in rows])
    arrivals = np.array([r["arrival_mono"] for r in rows])
    ticks = np.arange(t0 + 1.0, t1, 1.0 / control_hz)  # skip the first second (model warm-up)
    idx = np.searchsorted(pubs, ticks, side="right") - 1
    valid = idx >= 0
    age_pub = np.where(valid, ticks - pubs[np.clip(idx, 0, None)], np.inf)
    age_cap = np.where(valid, ticks - arrivals[np.clip(idx, 0, None)], np.inf)
    out = {"ticks": int(len(ticks)), "control_hz": control_hz, "stale_s": stale_s}
    for name, age in (("stamp_at_publish", age_pub), ("stamp_at_capture", age_cap)):
        stale = age > stale_s
        # guard (mirrors the autodrone L4 guard): remember the first stale tick, STOP once now - first >= grace_s;
        # count each such episode once
        first, stops = None, 0
        for t, s in zip(ticks, stale):
            if not s:
                first = None
                continue
            if first is None:
                first = t
            elif t - first >= grace_s - 1e-9:
                stops += 1
                first = np.inf  # episode counted; wait for a fresh tick before another can start
        out[name] = {"fresh_fraction": round(float(1 - stale.mean()), 3),
                     "age_ms": pct([a * 1000 for a in age[np.isfinite(age)]]),
                     "guard_stops": stops}
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("clip", type=Path)
    ap.add_argument("--events", type=Path, required=True)
    ap.add_argument("--config", type=Path, default=ROOT / "laptop.toml")
    ap.add_argument("--model", default=str(ROOT / "models" / "yolo11n.pt"))
    ap.add_argument("--depth-model", default=str(ROOT / "models" / "da2-metric-indoor-small-hf"))
    ap.add_argument("--depth-hz", type=float, default=10.0)
    ap.add_argument("--depth-scale", type=float, default=1.0)
    ap.add_argument("--mode", choices=["realtime", "every"], default="realtime")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--stale-s", type=float, default=0.15)
    ap.add_argument("--control-hz", type=float, default=20.0)
    ap.add_argument("--grace-s", type=float, default=0.5, help="L4 guard: STOP after this long continuously stale")
    ap.add_argument("--max-frames", type=int, default=None)
    ap.add_argument("--freespace-udp", default=None, help="host:port for free-space datagrams (bridge test)")
    ap.add_argument("--tracks-udp", default=None, help="host:port for per-frame tracks datagrams (bridge test)")
    args = ap.parse_args(argv)

    cfg = load_config(args.config) if args.config else PipelineConfig()
    cfg = dataclasses.replace(cfg, storage=dataclasses.replace(cfg.storage, root=args.events),
                              detector=dataclasses.replace(cfg.detector, model=args.model),
                              depth=dataclasses.replace(cfg.depth, enabled=True, model=args.depth_model,
                                                        scale=args.depth_scale),
                              freespace=dataclasses.replace(cfg.freespace, hz=args.depth_hz,
                                                            udp=args.freespace_udp or cfg.freespace.udp),
                              tracks=dataclasses.replace(cfg.tracks, udp=args.tracks_udp or cfg.tracks.udp))
    realtime = args.mode == "realtime"
    if realtime and args.speed != 1.0:
        k = 1.0 / args.speed  # replay slowed k times: every seconds-valued window must stretch k times
        cfg = dataclasses.replace(
            cfg,
            gate=dataclasses.replace(cfg.gate, window_s=cfg.gate.window_s * k, lost_track_s=cfg.gate.lost_track_s * k,
                                     max_processing_gap_s=cfg.gate.max_processing_gap_s * k),
            queue=dataclasses.replace(cfg.queue, expiry_s=cfg.queue.expiry_s * k),
            confirm=dataclasses.replace(cfg.confirm, deadline_s=cfg.confirm.deadline_s * k,
                                        drain_s=cfg.confirm.drain_s * k,
                                        retry_cooldown_s=cfg.confirm.retry_cooldown_s * k))
    detector = make_detector(cfg.detector)
    depth = CpuDepth(args.depth_model, scale=args.depth_scale)
    probe = VideoFileSource(args.clip)  # size/fps only; warm-up must finish before the paced source starts
    src_w, src_h, src_fps = probe.width, probe.height, probe.fps
    probe.close()
    detector.warmup((src_h, src_w, 3))
    depth.depth_map(np.zeros((src_h, src_w, 3), np.uint8))  # warm-up
    source = (PacedSource if realtime else VideoFileSource)(args.clip, realtime=realtime, speed=args.speed)
    tracker = PersonTracker(dataclasses.replace(cfg.tracker, frame_rate=round(source.fps)))
    backend = StubBackend(lambda _r: '{"answer": "yes"}')  # no confirmer in the loop
    clock = time.monotonic if realtime else ReplayClock()
    extra = {"detector": {"model": detector.model_path, "imgsz": cfg.detector.imgsz, "tracker": "bytetrack"},
             "depth_model": args.depth_model, "mimic": "depth thread + detector thread, no Cosmos"}
    pipe = EventPipeline(cfg, backend, clock=clock, record_extra=extra, depth=depth)
    pipe.start() if realtime else pipe.open()
    session_dir = pipe.store.session_dir
    print(f"session {pipe.session_id} -> {session_dir}", flush=True)

    fs_thread = FreeSpaceStream(depth, cfg.freespace, pipe.health, log_path=session_dir / "freespace.jsonl",
                                scale=args.depth_scale)
    tracks_pub = TracksPublisher(cfg.tracks, pipe.health, depth_map_source=fs_thread.latest_map) if cfg.tracks.udp else None
    det_ms: deque[float] = deque()
    loop_ms: deque[float] = deque()
    age_ms: deque[float] = deque()
    processed = dropped = 0
    last_id = None
    started = time.monotonic()
    fs_thread.start()
    try:
        while True:
            if args.max_frames is not None and processed >= args.max_frames:
                break
            frame = source.read(timeout=1.0)
            if frame is None:
                if source.finished:
                    break
                continue
            t0 = time.monotonic()
            if last_id is None:
                dropped += frame.info.frame_id - 1  # superseded in the slot before the first read
            elif frame.info.frame_id > last_id + 1:
                dropped += frame.info.frame_id - last_id - 1
            last_id = frame.info.frame_id
            fs_thread.offer(frame)  # fan-out: the depth thread always sees the newest frame
            age_ms.append((t0 - frame.info.arrival_mono) * 1000)
            xyxy, conf = detector.detect(frame.image)
            dets = tracker.update(xyxy, conf, frame.info.frame_id)
            if isinstance(clock, ReplayClock):
                clock.set(frame)
            pipe.on_frame(frame, dets)
            if tracks_pub is not None:
                tracks_pub.publish(frame, dets)
            if not realtime:
                pipe.tick()
                while pipe.worker.state is WorkerState.ACTIVE:
                    time.sleep(0.002)
                    pipe.tick()
            processed += 1
            det_ms.append(detector.last_ms)
            loop_ms.append((time.monotonic() - t0) * 1000)
    finally:
        elapsed = time.monotonic() - started
        fs_thread.stop()
        if tracks_pub is not None:
            tracks_pub.close()
        if realtime:
            deadline = time.monotonic() + cfg.confirm.deadline_s
            while pipe.worker.state.value in ("active", "draining") and time.monotonic() < deadline:
                time.sleep(0.05)
        pipe.stop()
        source.close()

    events = [json.loads(p.read_text()) for p in sorted(session_dir.glob("*/event.json"))]
    dists = [e["distance_m"] for e in events if e.get("distance_m") is not None]
    snap = pipe.health.snapshot()
    rows = [json.loads(l) for l in (session_dir / "freespace.jsonl").read_text().splitlines()]
    for r in rows:  # the packaged log carries the map's age; recover arrival for the replay
        r["arrival_mono"] = r["t_pub_mono"] - r["age_at_publish_ms"] / 1000
    stream = fs_thread.summary()
    mimic = {
        "clip": str(args.clip), "mode": args.mode, "speed": args.speed, "elapsed_s": round(elapsed, 1),
        "detector": {"frames_processed": processed, "frames_dropped": dropped,
                     "processed_fps": round(processed / elapsed, 2), "detector_ms": pct(det_ms),
                     "loop_ms": pct(loop_ms), "frame_age_at_detect_ms": pct(age_ms)},
        "depth": {"target_hz": args.depth_hz, "achieved_hz": stream["achieved_hz"], "publishes": stream["publishes"],
                  "depth_ms": stream["depth_ms"], "age_at_publish_ms": stream["age_at_publish_ms"],
                  "input": [depth.height, depth.width], "source_size": [source.width, source.height],
                  "aspect_stretch": round((depth.width / source.width) / (depth.height / source.height), 3),
                  "scale": args.depth_scale, "calibrated": False, "max_depth_m": depth.meta.get("max_depth")},
        "free_space": {"min_m": stream["min_m"], "center_free": stream["center_free"],
                       "all_close_fraction": stream["all_close_fraction"], "params": stream["params"]},
        "controller": controller_freshness(rows, started, started + elapsed, args.control_hz, args.stale_s, args.grace_s),
        "events": {"count": len(events), "with_distance": len(dists), "distance_m": pct(dists),
                   "per_event_depth_ms": pct([e["depth"]["depth_ms"] for e in events if e.get("depth")])},
        "skips": {k: v for k, v in snap.get("counters", {}).items() if k.startswith("skip")},
        "windows_s": {"gate_window": cfg.gate.window_s, "lost_track": cfg.gate.lost_track_s,
                      "queue_expiry": cfg.queue.expiry_s, "deadline": cfg.confirm.deadline_s},
        "depth_thread": {"lock_wait_ms": pct(depth.wait_ms), "skipped_nonfinite": stream["skipped_nonfinite"]},
        "faults": snap.get("faults", {}),
    }
    (session_dir / "mimic_report.json").write_text(json.dumps(mimic, indent=2))
    # minimal run_report.json so autolabel / corvidia-encounter-eval / run_clip checks work on this session
    (session_dir / "run_report.json").write_text(json.dumps({
        "source": source.source_id, "mode": args.mode, "confirmer": "stub:yes", "frames_processed": processed,
        "frames_dropped": dropped, "processed_fps": mimic["detector"]["processed_fps"],
        "detector_ms": pct(det_ms), "events": len(events), "faults": snap.get("faults", {}),
        "mimic": True, "depth_model": args.depth_model}, indent=2))
    print(json.dumps(mimic, indent=2), flush=True)


if __name__ == "__main__":
    main()
