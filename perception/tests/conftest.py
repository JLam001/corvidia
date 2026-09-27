"""Deterministic detection-event fixtures driven by a fake monotonic clock."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import numpy as np
import pytest

from corvidia_perception.config import PipelineConfig
from corvidia_perception.confirmer import StubBackend
from corvidia_perception.pipeline import EventPipeline
from corvidia_perception.records import BBox, Detection, Frame, FrameInfo

W, H = 320, 240


class FakeClock:
    def __init__(self, t: float = 100.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def person(track_id: int, x: float = 40, conf: float = 0.8, w: float = 40, h: float = 90) -> Detection:
    return Detection(track_id, BBox(x, 60, x + w, 60 + h), conf)


class Harness:
    """Feeds frames at a fixed rate and ticks the worker between them."""

    def __init__(self, tmp_path: Path, backend: StubBackend | None = None,
                 cfg: PipelineConfig | None = None, fps: float = 10.0, **kwargs) -> None:
        cfg = cfg or PipelineConfig()
        cfg = dataclasses.replace(cfg, storage=dataclasses.replace(cfg.storage, root=tmp_path / "events"))
        self.clock = FakeClock()
        self.backend = backend or StubBackend()
        kwargs.setdefault("free_bytes", lambda _p: 10 * 1024**4)
        self.pipe = EventPipeline(cfg, self.backend, session_id="s1", clock=self.clock,
                                  wall=lambda: 1_700_000_000.0, **kwargs)
        self.pipe.open()
        self.dt = 1.0 / fps
        self.epoch = 0
        self.frame_id = 0
        # One reused buffer, like a camera ring buffer.
        self.image = np.zeros((H, W, 3), dtype=np.uint8)

    def frame(self, *dets: Detection, tick: bool = True) -> None:
        self.frame_id += 1
        self.image[:] = self.frame_id % 256
        info = FrameInfo("test", self.epoch, self.frame_id, W, H, self.clock(), 0.0)
        self.pipe.on_frame(Frame(info, self.image), list(dets))
        if tick:
            self.pipe.tick()
        self.clock.advance(self.dt)

    def frames(self, n: int, *dets: Detection, tick: bool = True) -> None:
        for _ in range(n):
            self.frame(*dets, tick=tick)

    def idle(self, seconds: float) -> None:
        """Let time pass with the worker running but no detections."""
        steps = max(1, round(seconds / self.dt))
        for _ in range(steps):
            self.frame()

    @property
    def session_dir(self) -> Path:
        return self.pipe.store.session_dir

    def events(self) -> list[dict]:
        return [json.loads(p.read_text()) for p in sorted(self.session_dir.glob("*/event.json"))]

    def skips(self) -> list[dict]:
        path = self.session_dir / "skips.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture
def harness(tmp_path):
    def make(**kwargs) -> Harness:
        return Harness(tmp_path, **kwargs)
    return make
