"""YOLO11n person detection with an explicitly configured ByteTrack tracker.

Detection and tracking are separate so that the tracker can be aged through
frames the detector never saw (real-time replay and live camera drop frames).
`.engine` models run on TensorRT directly (trt_detector.py); `.pt` models use
Ultralytics and PyTorch and are kept for comparison and export.
"""

from __future__ import annotations

import os
import time
import numpy as np

from .bytetrack import ByteTracker
from .config import DetectorConfig, TrackerConfig
from .records import BBox, Detection

PERSON_CLASS = 0

# Never let Ultralytics pip-install packages at runtime; dependencies are locked.
os.environ.setdefault("YOLO_AUTOINSTALL", "False")


class PersonTracker:
    """ByteTrack over person boxes, reporting the detector's own box and confidence."""

    def __init__(self, cfg: TrackerConfig) -> None:
        self.cfg = cfg
        self._tracker = ByteTracker(cfg)
        self._last_frame_id: int | None = None

    def reset(self) -> None:
        self._tracker.reset()
        self._last_frame_id = None

    def update(self, xyxy: np.ndarray, conf: np.ndarray, frame_id: int) -> list[Detection]:
        """Track one processed frame. `frame_id` gaps age the tracker through dropped frames."""
        if self._last_frame_id is not None:
            missed = min(frame_id - self._last_frame_id - 1, self.cfg.max_gap_frames)
            for _ in range(max(0, missed)):
                self._tracker.update(np.zeros((0, 4)), np.zeros(0))
        self._last_frame_id = frame_id

        xyxy = np.asarray(xyxy, dtype=np.float32).reshape(-1, 4)
        conf = np.asarray(conf, dtype=np.float32).reshape(-1)
        out: list[Detection] = []
        for row in self._tracker.update(xyxy, conf):
            # Crops use the detector's box for this frame, not the Kalman estimate.
            idx = int(row[6])
            out.append(Detection(int(row[4]), BBox(*map(float, xyxy[idx])), float(conf[idx])))
        return out


def make_detector(cfg: DetectorConfig):
    """TensorRT engines run without PyTorch; other formats go through Ultralytics."""
    if cfg.model.endswith(".engine"):
        from .trt_detector import TrtYoloDetector

        return TrtYoloDetector(cfg)
    return YoloDetector(cfg)


class YoloDetector:
    """Ultralytics/PyTorch YOLO on BGR frames; returns person boxes (xyxy, conf)."""

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
