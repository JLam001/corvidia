"""Pipeline settings. Defaults follow docs/event-pipeline.md and are not calibrated."""

from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PROMPT = (
    "Does this image visibly contain a real human, rather than only a picture, "
    "screen image, statue, or mannequin? Answer yes, no, or uncertain. Use uncertain "
    "when the image is too small, blurred, or ambiguous to decide."
)


@dataclass(frozen=True)
class GateConfig:
    min_hits: int = 3
    window_frames: int = 5
    window_s: float = 1.0
    min_confidence: float = 0.25
    # Observations older than this with no new detection end the encounter.
    lost_track_s: float = 1.5
    # A gap this long between processed frames clears all observation history.
    max_processing_gap_s: float = 1.0
    # A queued candidate whose track is lost is skipped (live mode).
    skip_lost_before_dispatch: bool = True


@dataclass(frozen=True)
class CropConfig:
    pad_fraction: float = 0.20
    min_width_px: int = 24
    min_height_px: int = 48
    # Variance of the Laplacian; 0 disables the blur check until tuned on footage.
    min_sharpness: float = 0.0
    # Channel order of frames from the source; OpenCV captures are BGR.
    color_order: str = "bgr"


@dataclass(frozen=True)
class QueueConfig:
    max_pending: int = 2
    expiry_s: float = 2.0


@dataclass(frozen=True)
class ConfirmConfig:
    deadline_s: float = 3.0
    # How long to wait for a timed-out request to finish or be cancelled before
    # declaring the backend unavailable.
    drain_s: float = 5.0
    max_unknown_retries: int = 1
    retry_cooldown_s: float = 5.0
    prompt: str = DEFAULT_PROMPT
    jpeg_quality: int = 95
    # Longest side of the image sent to the model. Bounds image tokens (Qwen3-VL:
    # about one token per 32x32 px); the saved crop.jpg is this exact image.
    max_image_side: int = 448


@dataclass(frozen=True)
class StorageConfig:
    root: Path = Path("~/corvidia-data/events").expanduser()
    save_frame: bool = True
    max_bytes: int = 2 * 1024**3
    min_free_bytes: int = 1 * 1024**3
    outcome_log_max: int = 256


@dataclass(frozen=True)
class TrackerConfig:
    """ByteTrack settings (Ultralytics names). Tune on labeled footage."""

    track_high_thresh: float = 0.25
    track_low_thresh: float = 0.1
    new_track_thresh: float = 0.25
    track_buffer: int = 30
    match_thresh: float = 0.8
    fuse_score: bool = True
    frame_rate: int = 30
    # Cap on empty updates used to age the tracker through dropped frames.
    max_gap_frames: int = 90


@dataclass(frozen=True)
class DetectorConfig:
    # A TensorRT engine runs without PyTorch; build it once with deploy/export_yolo.sh.
    model: str = "~/models/yolo11n.engine"
    device: str = "0"
    imgsz: int = 640
    # Low threshold so ByteTrack's second association stage sees weak boxes;
    # the candidate gate applies its own min_confidence.
    predict_conf: float = 0.1
    iou: float = 0.7
    half: bool = False


@dataclass(frozen=True)
class SystemConfig:
    # Raise a memory_low fault when available RAM drops below this. The Jetson's
    # CPU and GPU share 7.5 GB; keep real headroom, not just enough to fit.
    min_available_mb: int = 1536


@dataclass(frozen=True)
class PipelineConfig:
    gate: GateConfig = field(default_factory=GateConfig)
    crop: CropConfig = field(default_factory=CropConfig)
    queue: QueueConfig = field(default_factory=QueueConfig)
    confirm: ConfirmConfig = field(default_factory=ConfirmConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    detector: DetectorConfig = field(default_factory=DetectorConfig)
    tracker: TrackerConfig = field(default_factory=TrackerConfig)
    system: SystemConfig = field(default_factory=SystemConfig)


def load_config(path: str | Path) -> PipelineConfig:
    """Load a TOML file whose tables mirror PipelineConfig sections."""
    with open(path, "rb") as f:
        data = tomllib.load(f)
    sections = {}
    for f_ in dataclasses.fields(PipelineConfig):
        cls = f_.default_factory  # type: ignore[misc]
        values = dict(data.pop(f_.name, {}))
        unknown = set(values) - {x.name for x in dataclasses.fields(cls)}
        if unknown:
            raise ValueError(f"unknown [{f_.name}] settings: {sorted(unknown)}")
        if f_.name == "storage" and "root" in values:
            values["root"] = Path(values["root"]).expanduser()
        sections[f_.name] = cls(**values)
    if data:
        raise ValueError(f"unknown config sections: {sorted(data)}")
    return PipelineConfig(**sections)
