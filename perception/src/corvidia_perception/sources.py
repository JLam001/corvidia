"""Frame sources: recorded video (every frame or real-time) and the CSI camera.

Real-time sources run a capture thread that feeds a one-slot latest-frame buffer,
so a slow detector drops frames instead of building a backlog. Frame IDs keep
counting through dropped frames; the gaps age the tracker.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import numpy as np

from .health import Health
from .pipeline import LatestFrameSlot
from .records import ClockDomain, Frame, FrameInfo


class FrameSource(Protocol):
    source_id: str
    fps: float

    def read(self, timeout: float) -> Frame | None:
        """Next frame to process, or None if none arrived within `timeout`."""
        ...

    @property
    def finished(self) -> bool: ...

    def close(self) -> None: ...


def _cv2():
    import cv2

    return cv2


class VideoFileSource:
    """Replays a video file.

    realtime=False: every frame, in order, as fast as the consumer reads (accuracy).
    realtime=True: decoded at `fps * speed` on a thread; the consumer gets only the
    latest frame and superseded frames are dropped (timing behavior).
    """

    def __init__(self, path: str | Path, *, realtime: bool = False, speed: float = 1.0,
                 loop: bool = False, health: Health | None = None) -> None:
        cv2 = _cv2()
        self.path = str(path)
        self.source_id = f"video:{Path(path).name}"
        self._cap = cv2.VideoCapture(self.path)
        if not self._cap.isOpened():
            raise FileNotFoundError(f"cannot open video {path}")
        self.fps = float(self._cap.get(cv2.CAP_PROP_FPS)) or 30.0
        self.width = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.realtime = realtime
        self.speed = speed
        self.loop = loop
        self.health = health or Health()
        self.epoch = 0
        self._index = 0
        self._frame_id = 0
        self._eof = False
        self._stop = threading.Event()
        self._slot = LatestFrameSlot(self.health)
        self._thread: threading.Thread | None = None
        if realtime:
            self._thread = threading.Thread(target=self._run, daemon=True, name="video-capture")
            self._thread.start()

    @property
    def finished(self) -> bool:
        return self._eof

    def _decode(self) -> Frame | None:
        ok, image = self._cap.read()
        if not ok:
            if not self.loop:
                return None
            # Seeking back is a source reset: new epoch, tracker IDs are not continuous.
            self._cap.set(_cv2().CAP_PROP_POS_FRAMES, 0)
            self.epoch += 1
            self._index = 0
            ok, image = self._cap.read()
            if not ok:
                return None
        pts = self._index / self.fps
        self._index += 1
        self._frame_id += 1
        info = FrameInfo(
            self.source_id, self.epoch, self._frame_id, image.shape[1], image.shape[0],
            time.monotonic(), time.time(), capture_ts=pts, capture_clock=ClockDomain.VIDEO_PTS,
            capture_quality="container_pts", pts_s=pts,
        )
        self.health.incr("frames_captured")
        return Frame(info, image)

    def _run(self) -> None:
        period = 1.0 / (self.fps * self.speed)
        next_t = time.monotonic()
        while not self._stop.is_set():
            frame = self._decode()
            if frame is None:
                self._eof = True
                self._slot.put_end()
                return
            self._slot.put(frame)
            next_t += period
            delay = next_t - time.monotonic()
            if delay > 0:
                self._stop.wait(delay)
            else:
                next_t = time.monotonic()  # decoder fell behind; do not burst

    def read(self, timeout: float = 1.0) -> Frame | None:
        if not self.realtime:
            frame = self._decode()
            if frame is None:
                self._eof = True
            return frame
        return self._slot.take(timeout)

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2.0)
        self._cap.release()


# IMX477 sensor modes on this Jetson (from Argus): 0 = 3840x2160 @ 30, 1 = 1920x1080 @ 60.
SENSOR_MODES = {0: (3840, 2160), 1: (1920, 1080)}


def argus_pipeline(sensor_id: int = 0, sensor_mode: int = 1,
                   output_size: tuple[int, int] = (1920, 1080), fps: int = 30,
                   flip: int = 0) -> str:
    """Argus capture, VIC scaling (nvvidconv), BGR frames into an appsink named `sink`."""
    cw, ch = SENSOR_MODES[sensor_mode]
    ow, oh = output_size
    return (
        f"nvarguscamerasrc sensor-id={sensor_id} sensor-mode={sensor_mode} ! "
        f"video/x-raw(memory:NVMM),width={cw},height={ch},framerate={fps}/1 ! "
        f"nvvidconv flip-method={flip} ! video/x-raw,width={ow},height={oh},format=BGRx ! "
        "videoconvert ! video/x-raw,format=BGR ! "
        "appsink name=sink max-buffers=1 drop=true sync=false"
    )


class GstCapture:
    """Pulls BGR frames from a GStreamer pipeline with an appsink named `sink`.

    `read()` returns (ok, image, capture_mono): capture_mono is the buffer
    timestamp on the pipeline's monotonic clock, in time.monotonic() seconds.
    For nvarguscamerasrc that is when Argus delivered the frame (measured ~4.5 ms
    before Python receives it), not the start of exposure.
    """

    def __init__(self, description: str, timeout_s: float = 2.0) -> None:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        Gst.init(None)
        self._Gst = Gst
        self._timeout_ns = int(timeout_s * 1e9)
        self._pipeline = Gst.parse_launch(description)
        self._sink = self._pipeline.get_by_name("sink")
        self._opened = self._pipeline.set_state(Gst.State.PLAYING) != Gst.StateChangeReturn.FAILURE
        self._monotonic_clock = None

    def isOpened(self) -> bool:
        return self._opened

    def read(self) -> tuple[bool, np.ndarray | None, float | None]:
        Gst = self._Gst
        sample = self._sink.emit("try-pull-sample", self._timeout_ns)
        if sample is None:
            return False, None, None
        caps = sample.get_caps().get_structure(0)
        w, h = caps.get_value("width"), caps.get_value("height")
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return False, None, None
        try:
            image = np.frombuffer(info.data, np.uint8, count=w * h * 3).reshape(h, w, 3).copy()
        finally:
            buf.unmap(info)
        capture = None
        if buf.pts != Gst.CLOCK_TIME_NONE:
            if self._monotonic_clock is None:
                clock = self._pipeline.get_clock()
                self._monotonic_clock = bool(
                    clock is not None and clock.props.clock_type == Gst.ClockType.MONOTONIC)
            if self._monotonic_clock:
                capture = (self._pipeline.get_base_time() + buf.pts) / 1e9
        return True, image, capture

    def release(self) -> None:
        self._pipeline.set_state(self._Gst.State.NULL)


class CameraSource:
    """Live capture (Argus CSI through GStreamer by default).

    A failed read is camera loss: the capture is reopened after `reconnect_s` and
    a new source epoch starts. Frames carry the driver's buffer timestamp as the
    capture time when available, else host arrival time.
    """

    def __init__(self, pipeline: str, *, source_id: str = "csi:0", fps: float = 30.0,
                 reconnect_s: float = 1.0, health: Health | None = None,
                 open_capture: Callable[[str], object] | None = None) -> None:
        self.source_id = source_id
        self.fps = fps
        self.pipeline = pipeline
        self.reconnect_s = reconnect_s
        self.health = health or Health()
        self.epoch = 0
        self._open = open_capture or GstCapture
        self._frame_id = 0
        self._slot = LatestFrameSlot(self.health)
        self._stop = threading.Event()
        self.connected = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="camera-capture")
        self._thread.start()

    @property
    def finished(self) -> bool:
        return False

    def _run(self) -> None:
        first = True
        while not self._stop.is_set():
            try:
                cap = self._open(self.pipeline)
            except Exception as e:  # noqa: BLE001 - pipeline construction errors are camera faults
                self.health.incr("camera_open_failures")
                self.health.fault("camera", f"cannot build capture: {e}")
                self._stop.wait(self.reconnect_s)
                continue
            if not cap.isOpened():
                self.health.incr("camera_open_failures")
                self.health.fault("camera", "cannot open capture")
                cap.release()
                self._stop.wait(self.reconnect_s)
                continue
            if not first:
                self.epoch += 1
            first = False
            self.connected.set()
            self.health.clear_fault("camera")
            while not self._stop.is_set():
                ok, image, capture = cap.read()
                if not ok or image is None:
                    self.health.incr("camera_lost")
                    self.health.fault("camera", "read failed; reconnecting")
                    break
                self._frame_id += 1
                now = time.monotonic()
                if capture is not None and capture <= now:
                    self.health.set("capture_to_arrival_ms", round((now - capture) * 1000, 1))
                    ts, quality = capture, "argus_buffer_pts"
                else:
                    ts, quality = now, "host_arrival"
                info = FrameInfo(
                    self.source_id, self.epoch, self._frame_id, image.shape[1], image.shape[0],
                    now, time.time(), capture_ts=ts, capture_clock=ClockDomain.HOST_MONOTONIC,
                    capture_quality=quality,
                )
                self.health.incr("frames_captured")
                self._slot.put(Frame(info, image))
            self.connected.clear()
            cap.release()
            self._stop.wait(self.reconnect_s)

    def read(self, timeout: float = 1.0) -> Frame | None:
        return self._slot.take(timeout)

    def close(self) -> None:
        self._stop.set()
        self._slot.put_end()
        self._thread.join(3.0)
