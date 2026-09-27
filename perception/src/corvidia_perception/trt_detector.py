"""YOLO person detection on a TensorRT engine, without PyTorch or Ultralytics.

Buffers are mapped pinned memory (trt_runtime.py). On Jetson the CPU and GPU
share DRAM, so the letterboxed frame is written straight into the engine's
input with no copy.
Pre/post-processing matches Ultralytics (letterbox with gray padding, class
argmax filter, IoU NMS) so results agree with the `.pt` model.
"""

from __future__ import annotations

import os
import time

import cv2
import numpy as np

from .config import DetectorConfig
from .trt_runtime import TrtEngine

PERSON_CLASS = 0


def letterbox(image: np.ndarray, size: int) -> tuple[np.ndarray, float, tuple[float, float]]:
    """Resize keeping aspect ratio and pad to size x size (Ultralytics LetterBox, centered)."""
    h, w = image.shape[:2]
    r = min(size / h, size / w)
    nw, nh = round(w * r), round(h * r)
    dw, dh = (size - nw) / 2, (size - nh) / 2
    if (w, h) != (nw, nh):
        image = cv2.resize(image, (nw, nh), interpolation=cv2.INTER_LINEAR)
    top, bottom = round(dh - 0.1), round(dh + 0.1)
    left, right = round(dw - 0.1), round(dw + 0.1)
    image = cv2.copyMakeBorder(image, top, bottom, left, right, cv2.BORDER_CONSTANT,
                               value=(114, 114, 114))
    return image, r, (left, top)


def nms(boxes: np.ndarray, scores: np.ndarray, iou_thresh: float) -> np.ndarray:
    """Greedy IoU NMS; returns kept indices in descending score order."""
    order = np.argsort(-scores, kind="stable")
    x1, y1, x2, y2 = boxes.T
    areas = (x2 - x1) * (y2 - y1)
    keep = []
    while order.size:
        i = order[0]
        keep.append(i)
        rest = order[1:]
        w = np.clip(np.minimum(x2[i], x2[rest]) - np.maximum(x1[i], x1[rest]), 0, None)
        h = np.clip(np.minimum(y2[i], y2[rest]) - np.maximum(y1[i], y1[rest]), 0, None)
        inter = w * h
        iou = inter / (areas[i] + areas[rest] - inter + 1e-7)
        order = rest[iou <= iou_thresh]
    return np.asarray(keep, dtype=int)


def decode(pred: np.ndarray, conf: float, iou: float, max_det: int = 300) -> tuple[np.ndarray, np.ndarray]:
    """(84, N) YOLO output -> person boxes (xyxy, letterbox pixels) and scores."""
    cls_scores = pred[4:]
    best = cls_scores.argmax(0)
    score = cls_scores.max(0)
    keep = (best == PERSON_CLASS) & (score > conf)
    if not keep.any():
        return np.zeros((0, 4), np.float32), np.zeros(0, np.float32)
    cx, cy, w, h = pred[:4, keep]
    boxes = np.stack([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], 1)
    score = score[keep]
    idx = nms(boxes, score, iou)[:max_det]
    return boxes[idx].astype(np.float32), score[idx].astype(np.float32)


class TrtYoloDetector:
    """Same interface as YoloDetector: detect(bgr) -> (xyxy, conf), plus last_ms."""

    def __init__(self, cfg: DetectorConfig) -> None:
        self.cfg = cfg
        self.model_path = os.path.expanduser(cfg.model)
        self._trt = TrtEngine(self.model_path)
        self.metadata = self._trt.metadata
        self.input_name = self._trt.input_names[0]
        self.output_name = self._trt.output_names[0]
        self.imgsz = int(self._trt.buffers[self.input_name].shape[-1])
        if cfg.imgsz != self.imgsz:
            raise ValueError(f"engine was built for imgsz={self.imgsz}, config says {cfg.imgsz}")
        self.last_ms = 0.0

    def warmup(self, shape: tuple[int, int, int], n: int = 3) -> None:
        blank = np.zeros(shape, dtype=np.uint8)
        for _ in range(n):
            self.detect(blank)

    def detect(self, image: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        t = time.perf_counter()
        boxed, r, (pad_x, pad_y) = letterbox(image, self.imgsz)
        inp = self._trt.buffers[self.input_name]
        # BGR HWC uint8 -> RGB CHW float in [0, 1], written into the mapped buffer.
        np.multiply(boxed[:, :, ::-1].transpose(2, 0, 1), 1 / 255.0, out=inp[0], casting="unsafe")
        self._trt.execute()
        pred = self._trt.buffers[self.output_name][0]
        boxes, scores = decode(pred, self.cfg.predict_conf, self.cfg.iou)
        if len(boxes):
            boxes[:, [0, 2]] = (boxes[:, [0, 2]] - pad_x) / r
            boxes[:, [1, 3]] = (boxes[:, [1, 3]] - pad_y) / r
            h, w = image.shape[:2]
            boxes[:, [0, 2]] = boxes[:, [0, 2]].clip(0, w)
            boxes[:, [1, 3]] = boxes[:, [1, 3]].clip(0, h)
        self.last_ms = (time.perf_counter() - t) * 1000
        return boxes, scores

    def close(self) -> None:
        self._trt.close()
