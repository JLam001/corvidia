"""Run the threaded pipeline on synthetic detections with a delayed stub confirmer.

    uv run corvidia-simulate --events /tmp/events --seconds 5
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import time
from pathlib import Path

import numpy as np

from .config import PipelineConfig, load_config
from .confirmer import DelayedStubBackend
from .pipeline import EventPipeline
from .records import BBox, ClockDomain, Detection, Frame, FrameInfo


def synthetic_detections(frame_id: int, fps: float) -> list[Detection]:
    """Track 1 persists; track 2 flickers; track 3 appears after two seconds."""
    t = frame_id / fps
    dets = [Detection(1, BBox(100 + t * 10, 120, 180 + t * 10, 330), 0.82)]
    if frame_id % 4 == 0:
        dets.append(Detection(2, BBox(400, 100, 450, 220), 0.40))
    if t >= 2.0:
        dets.append(Detection(3, BBox(300, 200, 360, 360), 0.71))
    return dets


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", type=Path)
    ap.add_argument("--events", type=Path, default=Path("events"))
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--fps", type=float, default=15.0)
    ap.add_argument("--inference-delay", type=float, default=0.4)
    args = ap.parse_args(argv)

    cfg = load_config(args.config) if args.config else PipelineConfig()
    cfg = dataclasses.replace(cfg, storage=dataclasses.replace(cfg.storage, root=args.events))
    backend = DelayedStubBackend(args.inference_delay, lambda _req: '{"answer": "yes"}')
    pipe = EventPipeline(cfg, backend)
    pipe.start()

    rng = np.random.default_rng(0)
    image = rng.integers(0, 255, (480, 640, 3), dtype=np.uint8)
    n_frames = int(args.seconds * args.fps)
    t0 = time.monotonic()
    for i in range(n_frames):
        image[:] = rng.integers(0, 255, image.shape, dtype=np.uint8)  # reuse the buffer like a camera
        info = FrameInfo("synthetic", 0, i, 640, 480, time.monotonic(), time.time(),
                         capture_clock=ClockDomain.HOST_MONOTONIC, capture_ts=time.monotonic())
        pipe.on_frame(Frame(info, image), synthetic_detections(i, args.fps))
        time.sleep(max(0.0, t0 + (i + 1) / args.fps - time.monotonic()))
    time.sleep(args.inference_delay + 0.2)
    pipe.stop()

    print(json.dumps(pipe.health.snapshot(), indent=2, default=str))
    print(f"session: {pipe.store.session_dir if pipe.store else None}")
    for c in pipe.completions:
        print(f"  track {c.key.track_id}: {c.result.value} ({c.reason}) {c.event_id}")


if __name__ == "__main__":
    main()
