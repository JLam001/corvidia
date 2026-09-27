"""Keep the clearest image of each tracked person and save it when the track ends.

The event's crop.jpg is taken the moment a track first qualifies, which is often
blurred or cut off. While the track lives, this keeps one full-resolution crop
with the best score (confidence x sqrt(box area) x sharpness term, halved when
the box touches the frame edge) and writes it as best.jpg / best.json into the
track's latest event directory once the track ends.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import cv2
import numpy as np

from .config import BestShotConfig, CropConfig
from .crop import freeze, padded_region, sharpness
from .records import CandidateKey, Detection, Frame


@dataclass
class _Shot:
    score: float = -1.0
    crop: np.ndarray | None = None
    meta: dict = field(default_factory=dict)
    event_id: str | None = None
    seen: int = 0


@dataclass(frozen=True)
class BestShot:
    event_id: str
    crop: np.ndarray
    meta: dict


def shot_score(image: np.ndarray, det: Detection, frame_w: int, frame_h: int) -> tuple[float, dict]:
    b = det.bbox
    area = max(b.width, 1.0) * max(b.height, 1.0)
    h = max(1, int(b.height))
    scale = 128 / h
    small = cv2.resize(image, (max(1, int(image.shape[1] * scale)), 128), interpolation=cv2.INTER_AREA)
    sharp = sharpness(small)
    at_edge = b.x1 <= 2 or b.y1 <= 2 or b.x2 >= frame_w - 2 or b.y2 >= frame_h - 2
    score = det.confidence * math.sqrt(area) * (sharp / (sharp + 100.0)) * (0.5 if at_edge else 1.0)
    return score, {"confidence": round(det.confidence, 3), "box_area_px": round(area),
                   "sharpness": round(sharp, 1), "at_frame_edge": at_edge}


class BestShotTracker:
    def __init__(self, cfg: BestShotConfig, crop_cfg: CropConfig, min_confidence: float) -> None:
        self.cfg = cfg
        self.crop_cfg = crop_cfg
        self.min_confidence = min_confidence
        self._shots: dict[CandidateKey, _Shot] = {}

    def update(self, frame: Frame, detections: list[Detection], tracks: dict, session_id: str) -> None:
        """Called on the detector thread after the gate has processed the frame."""
        info = frame.info
        for det in detections:
            if det.confidence < self.min_confidence:
                continue
            key = CandidateKey(session_id, info.source_epoch, det.track_id)
            state = tracks.get(key)
            if state is None:
                continue
            shot = self._shots.get(key)
            if shot is None:
                if len(self._shots) >= self.cfg.max_tracks:
                    continue
                shot = self._shots[key] = _Shot()
            if state.event_id:
                shot.event_id = state.event_id
            shot.seen += 1
            if (shot.seen - 1) % self.cfg.every_n_frames:
                continue
            region = padded_region(det.bbox, info.width, info.height, self.crop_cfg.pad_fraction)
            if region is None:
                continue
            view = frame.image[region.y1:region.y2, region.x1:region.x2]
            score, meta = shot_score(view, det, info.width, info.height)
            if score > shot.score:
                shot.score = score
                shot.crop = freeze(view)
                shot.meta = {**meta, "score": round(score, 2), "frame_id": info.frame_id,
                             "capture_ts": info.capture_ts, "crop_region": region.as_list(),
                             "bbox": det.bbox.as_list(), "track_id": det.track_id}

    def finished(self, tracks: dict) -> list[BestShot]:
        """Best shots of tracks the gate no longer holds."""
        done = [k for k in self._shots if k not in tracks]
        return self._take(done)

    def flush(self) -> list[BestShot]:
        return self._take(list(self._shots))

    def _take(self, keys) -> list[BestShot]:
        out = []
        for key in keys:
            shot = self._shots.pop(key)
            if shot.event_id and shot.crop is not None:
                out.append(BestShot(shot.event_id, shot.crop, shot.meta))
        return out
