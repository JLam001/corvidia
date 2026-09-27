"""YOLO11n person detection with an explicitly configured ByteTrack tracker.

Detection and tracking are separate so that the tracker can be aged through
frames the detector never saw (real-time replay and live camera drop frames).
"""

from __future__ import annotations

import os
import time
from dataclasses import asdict
from types import SimpleNamespace

import numpy as np

from .config import DetectorConfig, TrackerConfig
from .records import BBox, Detection

PERSON_CLASS = 0

# Never let Ultralytics pip-install packages at runtime; dependencies are locked.
os.environ.setdefault("YOLO_AUTOINSTALL", "False")


class _Dets:
    """Minimal stand-in for Ultralytics Boxes as consumed by BYTETracker.update."""

    def __init__(self, xyxy: np.ndarray, conf: np.ndarray) -> None:
        self.xyxy = xyxy.reshape(-1, 4).astype(np.float32)
        self.conf = conf.reshape(-1).astype(np.float32)
        self.cls = np.zeros_like(self.conf)

    @property
    def xywh(self) -> np.ndarray:
        xy = (self.xyxy[:, :2] + self.xyxy[:, 2:]) / 2
        wh = self.xyxy[:, 2:] - self.xyxy[:, :2]
        return np.concatenate([xy, wh], axis=1)

    def __len__(self) -> int:
        return len(self.conf)

    def __getitem__(self, idx) -> _Dets:
        return _Dets(self.xyxy[idx], self.conf[idx])


def _iou(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """IoU between one box `a` (4,) and boxes `b` (N, 4)."""
    x1 = np.maximum(a[0], b[:, 0])
    y1 = np.maximum(a[1], b[:, 1])
    x2 = np.minimum(a[2], b[:, 2])
    y2 = np.minimum(a[3], b[:, 3])
    inter = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1])
    return inter / np.maximum(area_a + area_b - inter, 1e-9)


class PersonTracker:
    """ByteTrack over person boxes, reporting the matched detector box and confidence."""

    def __init__(self, cfg: TrackerConfig) -> None:
        from ultralytics.trackers.byte_tracker import BYTETracker

        self.cfg = cfg
        args = SimpleNamespace(tracker_type="bytetrack", **{
            k: v for k, v in asdict(cfg).items() if k not in ("frame_rate", "max_gap_frames")
        })
        self._tracker = BYTETracker(args, frame_rate=cfg.frame_rate)
        self._last_frame_id: int | None = None

    def reset(self) -> None:
        self._tracker.reset()
        self._last_frame_id = None

    def update(self, xyxy: np.ndarray, conf: np.ndarray, frame_id: int) -> list[Detection]:
        """Track one processed frame. `frame_id` gaps age the tracker through dropped frames."""
        if self._last_frame_id is not None:
            missed = min(frame_id - self._last_frame_id - 1, self.cfg.max_gap_frames)
            empty = _Dets(np.zeros((0, 4)), np.zeros(0))
            for _ in range(max(0, missed)):
                self._tracker.update(empty)
        self._last_frame_id = frame_id

        dets = _Dets(xyxy, conf)
        tracks = self._tracker.update(dets)
        out: list[Detection] = []
        for row in tracks:
            box, track_id, score = row[:4], int(row[4]), float(row[5])
            # Report the detector's own box (not the Kalman estimate) for crops;
            # the matched detection has the same score and the highest overlap.
            if len(dets):
                same = np.isclose(dets.conf, score, atol=1e-6)
                cand = np.flatnonzero(same) if same.any() else np.arange(len(dets))
                best = cand[np.argmax(_iou(box, dets.xyxy[cand]))]
                box, score = dets.xyxy[best], float(dets.conf[best])
            out.append(Detection(track_id, BBox(*map(float, box)), score))
        return out


class YoloDetector:
    """Runs YOLO on BGR frames and returns person boxes (xyxy, conf) on the CPU."""

    def __init__(self, cfg: DetectorConfig) -> None:
        from ultralytics import YOLO

        self.cfg = cfg
        self.model_path = os.path.expanduser(cfg.model)
        self._model = YOLO(self.model_path, task="detect")
        self.last_ms = 0.0

    def warmup(self, shape: tuple[int, int, int], n: int = 3) -> None:
        blank = np.zeros(shape, dtype=np.uint8)
        for _ in range(n):
            self.detect(blank)

    def detect(self, image: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        t = time.perf_counter()
        r = self._model.predict(
            image, device=self.cfg.device, imgsz=self.cfg.imgsz, conf=self.cfg.predict_conf,
            iou=self.cfg.iou, half=self.cfg.half, classes=[PERSON_CLASS], verbose=False,
        )[0]
        boxes = r.boxes
        xyxy = boxes.xyxy.cpu().numpy() if len(boxes) else np.zeros((0, 4), np.float32)
        conf = boxes.conf.cpu().numpy() if len(boxes) else np.zeros(0, np.float32)
        self.last_ms = (time.perf_counter() - t) * 1000
        return xyxy, conf
