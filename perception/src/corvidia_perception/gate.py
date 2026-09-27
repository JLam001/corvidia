"""Candidate lifecycle: persistence gate, crop selection, admission, and suppression.

The gate is owned by the detector thread. Completions and skips from the
confirmation worker are applied through `apply_completion` / `apply_skip`.
"""

from __future__ import annotations

import enum
import uuid
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from .config import PipelineConfig
from .confirm_queue import ConfirmationQueue
from .crop import bbox_passes_size, freeze, select_crop
from .health import Health
from .records import Candidate, CandidateKey, Completion, Detection, Frame, Result, Skip, SkipReason


class Phase(enum.StrEnum):
    OBSERVING = "observing"
    PENDING = "pending"          # queued or confirming; exactly one event in flight
    COOLDOWN = "cooldown"        # unknown result; one retry allowed after cooldown
    SUPPRESSED = "suppressed"    # encounter finished; no more requests for this track


@dataclass
class TrackState:
    key: CandidateKey
    phase: Phase = Phase.OBSERVING
    # (processed frame sequence, host monotonic time) of qualifying detections
    obs: deque[tuple[int, float]] = field(default_factory=deque)
    last_seen: float = 0.0
    lost: bool = False
    unknowns: int = 0
    cooldown_until: float = 0.0
    event_id: str | None = None


class CandidateGate:
    def __init__(self, cfg: PipelineConfig, queue: ConfirmationQueue, health: Health,
                 session_id: str, accepting: Callable[[], bool] = lambda: True) -> None:
        self._cfg = cfg
        self._queue = queue
        self._health = health
        self._accepting = accepting
        self.session_id = session_id
        self.epoch: int | None = None
        self.tracks: dict[CandidateKey, TrackState] = {}
        self._seq = 0
        self._last_processed: float | None = None

    # -- detector-thread entry points -------------------------------------------------

    def update(self, frame: Frame, detections: Sequence[Detection], now: float) -> list[Skip]:
        """Process one detector output. Returns skips produced on this thread."""
        g = self._cfg.gate
        skips: list[Skip] = []

        if frame.info.source_epoch != self.epoch:
            skips += self.reset_source(frame.info.source_epoch, now)
        if self._last_processed is not None and now - self._last_processed > g.max_processing_gap_s:
            self._health.incr("processing_gaps")
            for st in self.tracks.values():
                st.obs.clear()
        self._last_processed = now
        self._seq += 1

        # Any tracked detection keeps the encounter alive (the tracker also follows
        # low-score boxes); only confident, large-enough ones count as evidence.
        seen: dict[CandidateKey, Detection] = {}
        for det in detections:
            if det.class_name != "person":
                continue
            key = CandidateKey(self.session_id, frame.info.source_epoch, det.track_id)
            prev = seen.get(key)
            if prev is None or det.confidence > prev.confidence:
                seen[key] = det

        for key, det in seen.items():
            st = self.tracks.get(key)
            if st is None:
                st = self.tracks[key] = TrackState(key)
            st.last_seen = now
            st.lost = False
            if det.confidence >= g.min_confidence and bbox_passes_size(det.bbox, self._cfg.crop):
                st.obs.append((self._seq, now))

        skips += self._expire_lost(now)
        for st in self.tracks.values():
            self._prune(st, now)

        for key, det in seen.items():
            st = self.tracks[key]
            if st.phase is Phase.COOLDOWN and now >= st.cooldown_until:
                st.phase = Phase.OBSERVING
            if st.phase is Phase.OBSERVING and self._eligible(st, now):
                self._try_admit(st, frame, det, now)
        self._health.set("tracks", len(self.tracks))
        return skips

    def reset_source(self, new_epoch: int, now: float) -> list[Skip]:
        """Camera reconnect or video seek: forget tracks and drop queued work."""
        self.epoch = new_epoch
        self.tracks.clear()
        self._last_processed = None
        self._health.incr("source_resets")
        return [
            Skip(c.key, c.event_id, SkipReason.SOURCE_RESET, now)
            for c in self._queue.remove_epoch_other_than(new_epoch)
        ]

    def source_lost(self, now: float) -> None:
        """Camera loss: observation history is no longer continuous."""
        for st in self.tracks.values():
            st.obs.clear()
        self._last_processed = None

    # -- results from the confirmation worker -----------------------------------------

    def apply_completion(self, c: Completion, now: float) -> None:
        st = self.tracks.get(c.key)
        if st is None or st.event_id != c.event_id:
            return
        st.event_id = None
        st.obs.clear()
        if c.result is Result.UNKNOWN and st.unknowns < self._cfg.confirm.max_unknown_retries:
            st.unknowns += 1
            st.phase = Phase.COOLDOWN
            st.cooldown_until = now + self._cfg.confirm.retry_cooldown_s
        else:
            st.phase = Phase.SUPPRESSED
        if st.lost:
            del self.tracks[c.key]

    def apply_skip(self, s: Skip) -> None:
        st = self.tracks.get(s.key)
        if st is None or st.event_id != s.event_id:
            return
        st.event_id = None
        st.obs.clear()  # require fresh evidence
        st.phase = Phase.OBSERVING
        if st.lost:
            del self.tracks[s.key]

    # -- internals --------------------------------------------------------------------

    def _prune(self, st: TrackState, now: float) -> None:
        g = self._cfg.gate
        min_seq = self._seq - g.window_frames + 1
        while st.obs and (st.obs[0][0] < min_seq or now - st.obs[0][1] > g.window_s):
            st.obs.popleft()

    def _eligible(self, st: TrackState, now: float) -> bool:
        # _prune has already restricted obs to the frame and time windows.
        return len(st.obs) >= self._cfg.gate.min_hits and st.obs[-1][0] == self._seq

    def _expire_lost(self, now: float) -> list[Skip]:
        skips: list[Skip] = []
        for key, st in list(self.tracks.items()):
            if st.lost or now - st.last_seen <= self._cfg.gate.lost_track_s:
                continue
            st.obs.clear()
            if st.phase is Phase.PENDING and st.event_id is not None:
                if self._cfg.gate.skip_lost_before_dispatch:
                    queued = self._queue.remove(key)
                    if queued is not None:
                        skips.append(Skip(key, queued.event_id, SkipReason.TRACK_LOST, now))
                        del self.tracks[key]
                        continue
                # Already dispatched: keep state until the result arrives.
                st.lost = True
                continue
            del self.tracks[key]
        return skips

    def _try_admit(self, st: TrackState, frame: Frame, det: Detection, now: float) -> None:
        if not self._accepting():
            self._health.incr("admission_blocked_storage")
            return
        if not self._queue.has_room(st.key):
            self._health.incr("admission_blocked_queue_full")
            return
        selected = select_crop(frame.image, det.bbox, self._cfg.crop)
        if selected is None:
            self._health.incr("crop_quality_rejects")
            return
        region, crop = selected
        candidate = Candidate(
            key=st.key,
            event_id=str(uuid.uuid4()),
            frame_info=frame.info,
            detection=det,
            crop_region=region,
            crop=crop,
            frame=freeze(frame.image) if self._cfg.storage.save_frame else None,
            queued_mono=now,
            attempt=st.unknowns + 1,
        )
        if not self._queue.admit(candidate):  # lost a race with nothing; defensive
            self._health.incr("admission_blocked_queue_full")
            return
        st.phase = Phase.PENDING
        st.event_id = candidate.event_id
        self._health.incr("candidates_queued")
