"""YOLO person detection on a TensorRT engine, without PyTorch or Ultralytics.

Buffers are CUDA managed memory. On Jetson the CPU and GPU share DRAM, so the
letterboxed frame is written straight into the engine's input with no copy.
Pre/post-processing matches Ultralytics (letterbox with gray padding, class
argmax filter, IoU NMS) so results agree with the `.pt` model.
"""

from __future__ import annotations

import ctypes
import json
import os
import struct
import time

import cv2
import numpy as np

from .config import DetectorConfig

PERSON_CLASS = 0
_MANAGED_ATTACH_GLOBAL = 1


class _CudaRuntime:
    def __init__(self) -> None:
        self.lib = ctypes.CDLL(os.environ.get("CUDART_PATH", "/usr/local/cuda/lib64/libcudart.so"))

    def check(self, err: int, what: str) -> None:
        if err != 0:
            self.lib.cudaGetErrorString.restype = ctypes.c_char_p
            raise RuntimeError(f"{what} failed: {self.lib.cudaGetErrorString(err).decode()}")

    def malloc_managed(self, nbytes: int) -> int:
        ptr = ctypes.c_void_p()
        self.check(self.lib.cudaMallocManaged(ctypes.byref(ptr), ctypes.c_size_t(nbytes),
                                              ctypes.c_uint(_MANAGED_ATTACH_GLOBAL)), "cudaMallocManaged")
        return ptr.value

    def free(self, ptr: int) -> None:
        self.lib.cudaFree(ctypes.c_void_p(ptr))

    def stream_create(self) -> int:
        stream = ctypes.c_void_p()
        self.check(self.lib.cudaStreamCreate(ctypes.byref(stream)), "cudaStreamCreate")
        return stream.value or 0

    def stream_sync(self, stream: int) -> None:
        self.check(self.lib.cudaStreamSynchronize(ctypes.c_void_p(stream)), "cudaStreamSynchronize")

    def stream_destroy(self, stream: int) -> None:
        self.lib.cudaStreamDestroy(ctypes.c_void_p(stream))


def read_engine(path: str) -> tuple[bytes, dict]:
    """Return the TensorRT plan and metadata, stripping an Ultralytics header if present."""
    data = open(path, "rb").read()
    n = struct.unpack("<I", data[:4])[0]
    if 0 < n < 1 << 20 and len(data) > 4 + n:
        try:
            meta = json.loads(data[4:4 + n])
            return data[4 + n:], meta
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    return data, {}


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
        import tensorrt as trt

        self.cfg = cfg
        self.model_path = os.path.expanduser(cfg.model)
        plan, self.metadata = read_engine(self.model_path)
        self._logger = trt.Logger(trt.Logger.WARNING)
        self._runtime = trt.Runtime(self._logger)
        self._engine = self._runtime.deserialize_cuda_engine(plan)
        if self._engine is None:
            raise RuntimeError(f"cannot deserialize TensorRT engine {self.model_path}")
        self._context = self._engine.create_execution_context()
        self._cuda = _CudaRuntime()
        self._stream = self._cuda.stream_create()
        self._buffers: dict[str, tuple[int, np.ndarray]] = {}
        self.input_name = self.output_name = ""
        for i in range(self._engine.num_io_tensors):
            name = self._engine.get_tensor_name(i)
            shape = tuple(self._engine.get_tensor_shape(name))
            dtype = np.dtype(trt.nptype(self._engine.get_tensor_dtype(name)))
            nbytes = int(np.prod(shape)) * dtype.itemsize
            ptr = self._cuda.malloc_managed(nbytes)
            view = np.ctypeslib.as_array((ctypes.c_byte * nbytes).from_address(ptr)).view(dtype).reshape(shape)
            self._buffers[name] = (ptr, view)
            self._context.set_tensor_address(name, ptr)
            if self._engine.get_tensor_mode(name) == trt.TensorIOMode.INPUT:
                self.input_name = name
            else:
                self.output_name = name
        self.imgsz = int(self._buffers[self.input_name][1].shape[-1])
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
        inp = self._buffers[self.input_name][1]
        # BGR HWC uint8 -> RGB CHW float in [0, 1], written into the managed buffer.
        np.multiply(boxed[:, :, ::-1].transpose(2, 0, 1), 1 / 255.0, out=inp[0], casting="unsafe")
        if not self._context.execute_async_v3(self._stream):
            raise RuntimeError("TensorRT execution failed")
        self._cuda.stream_sync(self._stream)
        pred = self._buffers[self.output_name][1][0]
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
        self._cuda.stream_sync(self._stream)
        for ptr, _ in self._buffers.values():
            self._cuda.free(ptr)
        self._buffers.clear()
        self._cuda.stream_destroy(self._stream)

    def __del__(self) -> None:
        try:
            if self._buffers:
                self.close()
        except Exception:  # noqa: BLE001
            pass
