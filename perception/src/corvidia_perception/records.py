"""Typed records passed between pipeline stages."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

import numpy as np

SCHEMA_VERSION = 1


class ClockDomain(enum.StrEnum):
    """Where a capture timestamp came from."""

    HOST_MONOTONIC = "host_monotonic"
    HOST_REALTIME = "host_realtime"
    SENSOR = "sensor"
    VIDEO_PTS = "video_pts"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class FrameInfo:
    """Identity and timing for one frame, independent of its pixels."""

    source_id: str
    source_epoch: int
    frame_id: int
    width: int
    height: int
    arrival_mono: float
    arrival_wall: float
    capture_ts: float | None = None
    capture_clock: ClockDomain = ClockDomain.UNAVAILABLE
    capture_quality: str = "unknown"
    pts_s: float | None = None


@dataclass(frozen=True, slots=True)
class Frame:
    """A frame and its pixels. The image may be overwritten by the source later."""

    info: FrameInfo
    image: np.ndarray


@dataclass(frozen=True, slots=True)
class BBox:
    """Pixel box in (x1, y1, x2, y2) form; x2/y2 are exclusive."""

    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def width(self) -> float:
        return self.x2 - self.x1

    @property
    def height(self) -> float:
        return self.y2 - self.y1

    def as_list(self) -> list[float]:
        return [self.x1, self.y1, self.x2, self.y2]


@dataclass(frozen=True, slots=True)
class Detection:
    track_id: int
    bbox: BBox
    confidence: float
    class_name: str = "person"


@dataclass(frozen=True, slots=True)
class CandidateKey:
    session_id: str
    source_epoch: int
    track_id: int


class Result(enum.StrEnum):
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class SkipReason(enum.StrEnum):
    QUEUE_EXPIRED = "queue_expired"
    TRACK_LOST = "track_lost"
    SOURCE_RESET = "source_reset"
    STORAGE_ERROR = "storage_error"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    SHUTDOWN = "shutdown"


@dataclass(frozen=True, slots=True)
class CropRegion:
    """Padded crop coordinates actually used, in integer pixels."""

    x1: int
    y1: int
    x2: int
    y2: int

    @property
    def width(self) -> int:
        return self.x2 - self.x1

    @property
    def height(self) -> int:
        return self.y2 - self.y1

    def as_list(self) -> list[int]:
        return [self.x1, self.y1, self.x2, self.y2]


@dataclass(frozen=True, slots=True)
class Candidate:
    """An eligible candidate with its frozen evidence, owned by the confirmation queue.

    `crop` and `frame` are private read-only copies; the camera cannot overwrite them.
    """

    key: CandidateKey
    event_id: str
    frame_info: FrameInfo
    detection: Detection
    crop_region: CropRegion
    crop: np.ndarray
    frame: np.ndarray | None
    queued_mono: float
    attempt: int


@dataclass(frozen=True, slots=True)
class Completion:
    """Final confirmation outcome for one dispatched candidate."""

    key: CandidateKey
    event_id: str
    result: Result
    reason: str
    # False when the final record could not be written; the event stays pending on
    # disk and is marked interrupted on restart.
    committed: bool = True


@dataclass(frozen=True, slots=True)
class Skip:
    """A candidate that left the queue without a confirmation request."""

    key: CandidateKey
    event_id: str
    reason: SkipReason
    mono: float
    detail: str = ""


@dataclass(slots=True)
class ConfirmRequest:
    request_id: str
    image_jpeg: bytes
    prompt: str
    metadata: dict = field(default_factory=dict)
