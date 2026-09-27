"""Metric monocular depth (Depth Anything V2 Metric Small) on TensorRT.

Runs once per confirmation request, on the event's frozen frame, not on every
camera frame. The distance to a person is the median depth over the torso part
of their box (central 40% of the width, 20-60% of the height), which avoids
background pixels around the silhouette.

Monocular metric depth is approximate and depends on how closely the camera
resembles the training data, so values are reported as "uncalibrated" until a
tape-measure check sets `depth.scale` and `depth.calibrated`.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import cv2
import numpy as np

from .config import DepthConfig
from .records import BBox
from .trt_runtime import TrtEngine

MEAN = np.array([0.485, 0.456, 0.406], np.float32)
STD = np.array([0.229, 0.224, 0.225], np.float32)


class DepthEstimator:
    def __init__(self, cfg: DepthConfig) -> None:
        self.cfg = cfg
        self.model_path = os.path.expanduser(cfg.model)
        self._trt = TrtEngine(self.model_path)
        self._in = self._trt.input_names[0]
        self._out = self._trt.output_names[0]
        _, _, self.height, self.width = self._trt.buffers[self._in].shape
        meta_path = Path(self.model_path).with_suffix(".json")
        self.meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        self.last_ms = 0.0
        # One engine context and one set of buffers: the free-space thread and the confirmation
        # worker (per-event distance) take turns instead of each paying for a second engine.
        self._lock = threading.Lock()

    def depth_map(self, bgr: np.ndarray) -> np.ndarray:
        """Depth in meters at the engine's resolution (height, width)."""
        small = cv2.resize(bgr, (self.width, self.height), interpolation=cv2.INTER_AREA)
        rgb = small[:, :, ::-1].astype(np.float32) * (1 / 255.0)
        with self._lock:
            # last_ms is the engine's own cost (buffer write + execute + copy), not the time
            # spent waiting for the other thread; it is set while the lock is still held.
            t = time.perf_counter()
            inp = self._trt.buffers[self._in]
            inp[0] = ((rgb - MEAN) / STD).transpose(2, 0, 1)
            self._trt.execute()
            out = self._trt.buffers[self._out].reshape(self.height, self.width).copy()
            self.last_ms = (time.perf_counter() - t) * 1000
        return out

    def person_distance(self, depth: np.ndarray, bbox: BBox, frame_w: int, frame_h: int) -> dict:
        sx, sy = self.width / frame_w, self.height / frame_h
        w, h = bbox.width, bbox.height
        x1 = int(np.floor((bbox.x1 + 0.3 * w) * sx))
        x2 = int(np.ceil((bbox.x1 + 0.7 * w) * sx))
        y1 = int(np.floor((bbox.y1 + 0.2 * h) * sy))
        y2 = int(np.ceil((bbox.y1 + 0.6 * h) * sy))
        x1, x2 = max(0, x1), min(self.width, max(x2, x1 + 1))
        y1, y2 = max(0, y1), min(self.height, max(y2, y1 + 1))
        roi = depth[y1:y2, x1:x2]
        roi = roi[np.isfinite(roi)]
        if roi.size == 0:
            return {"distance_m": None, "error": "empty depth region"}
        p25, p50, p75 = (float(v) * self.cfg.scale for v in np.percentile(roi, [25, 50, 75]))
        return {"distance_m": round(p50, 2), "roi_p25_m": round(p25, 2), "roi_p75_m": round(p75, 2),
                "roi_pixels": int(roi.size)}

    def measure(self, bgr: np.ndarray, bbox: BBox) -> dict:
        """Distance record for one person in one frame."""
        try:
            depth = self.depth_map(bgr)
            result = self.person_distance(depth, bbox, bgr.shape[1], bgr.shape[0])
        except Exception as e:  # noqa: BLE001 - depth must never block an event
            return {"distance_m": None, "error": f"{type(e).__name__}: {e}", "model": self.model_path}
        result.update(model=self.model_path, input=[self.height, self.width],
                      scale=self.cfg.scale, depth_ms=round(self.last_ms, 1),
                      status="calibrated" if self.cfg.calibrated else "uncalibrated")
        return result

    def close(self) -> None:
        self._trt.close()
