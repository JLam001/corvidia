"""Confirmation worker: evidence commit, one active request, deadlines, and recovery.

`tick(now)` is a non-blocking state machine step so tests can drive it with a
fake clock; `EventPipeline` runs it on a background thread.
"""

from __future__ import annotations

import enum
import hashlib
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass

from .config import PipelineConfig
from .confirm_queue import ConfirmationQueue
from .confirmer import ConfirmerBackend, normalize
from .evidence import EvidenceStore, StorageError, encode_jpeg
from .health import Health
from .records import SCHEMA_VERSION, Candidate, Completion, ConfirmRequest, Result, Skip, SkipReason


class WorkerState(enum.StrEnum):
    IDLE = "idle"
    ACTIVE = "active"
    DRAINING = "draining"        # timed out; waiting for completion or cancel ack
    UNAVAILABLE = "unavailable"  # backend not known to be idle; needs recover()


@dataclass
class _InFlight:
    candidate: Candidate
    record: dict
    future: Future[str]
    dispatched: float
    deadline: float
    # Host time; the pipeline clock may be paused video time during accuracy replay.
    dispatched_host: float = 0.0
    depth: dict | None = None


Emit = Callable[[Completion | Skip], None]


class ConfirmationWorker:
    def __init__(self, cfg: PipelineConfig, queue: ConfirmationQueue, backend: ConfirmerBackend,
                 store: EvidenceStore | None, health: Health, emit: Emit,
                 wall: Callable[[], float] = time.time, record_extra: dict | None = None,
                 depth=None, clock: Callable[[], float] | None = None) -> None:
        self._cfg = cfg
        # Pipeline clock, read again at submit so evidence writes do not eat the deadline.
        self._clock = clock
        self._depth = depth
        self._record_extra = record_extra or {}
        self._queue = queue
        self._backend = backend
        self._store = store
        self._health = health
        self._emit = emit
        self._wall = wall
        self._prompt_sha256 = hashlib.sha256(cfg.confirm.prompt.encode()).hexdigest()
        self.state = WorkerState.IDLE
        self._inflight: _InFlight | None = None
        self._draining_since = 0.0

    @property
    def backend_info(self):
        return self._backend.info

    @property
    def available(self) -> bool:
        return self.state is not WorkerState.UNAVAILABLE

    def tick(self, now: float) -> None:
        if self.state is WorkerState.ACTIVE:
            self._check_active(now)
        if self.state is WorkerState.DRAINING:
            self._check_draining(now)
        for c in self._queue.expire(now):
            self._skip(c, SkipReason.QUEUE_EXPIRED, now)
        if self.state is WorkerState.IDLE:
            candidate, expired = self._queue.pop(now)
            for c in expired:
                self._skip(c, SkipReason.QUEUE_EXPIRED, now)
            if candidate is not None:
                self._dispatch(candidate, now)
        self._health.set("worker_state", self.state.value)
        self._health.set("queue_depth", len(self._queue))

    def recover(self) -> bool:
        """Controlled recovery from UNAVAILABLE. Late results remain ignored."""
        if self.state is not WorkerState.UNAVAILABLE:
            return True
        if self._backend.reset():
            self.state = WorkerState.IDLE
            self._inflight = None
            self._health.clear_fault("backend")
            return True
        return False

    def shutdown(self, now: float) -> None:
        for c in self._queue.drain():
            self._skip(c, SkipReason.SHUTDOWN, now)
        if self._inflight is not None:
            self._backend.cancel(self._inflight.candidate.event_id)

    # -- internals ----------------------------------------------------------------------

    def _dispatch(self, c: Candidate, now: float) -> None:
        color = self._cfg.crop.color_order
        quality = self._cfg.confirm.jpeg_quality
        crop_jpeg = encode_jpeg(c.crop, quality, color, self._cfg.confirm.max_image_side)
        record = self._record(c, crop_jpeg, now)
        if self._store is not None:
            frame_jpeg = (encode_jpeg(c.frame, quality, color)
                          if c.frame is not None and self._cfg.storage.save_frame else None)
            try:
                self._store.commit_candidate(c.event_id, crop_jpeg, frame_jpeg, record)
            except StorageError as e:
                self._skip(c, SkipReason.STORAGE_ERROR, now, str(e))
                return
        request = ConfirmRequest(c.event_id, crop_jpeg, self._cfg.confirm.prompt,
                                 {"prompt_sha256": self._prompt_sha256})
        try:
            future = self._backend.submit(request)
        except Exception as e:  # noqa: BLE001 - submission failure is an unknown result
            failed: Future[str] = Future()
            failed.set_exception(e)
            future = failed
        self._health.incr("confirmations_dispatched")
        sent = self._clock() if self._clock is not None else now
        self._inflight = _InFlight(c, record, future, sent, sent + self._cfg.confirm.deadline_s,
                                   time.monotonic())
        if self._depth is not None and c.frame is not None:
            # Runs on this thread while the confirmer works on its own thread.
            self._inflight.depth = self._depth.measure(c.frame, c.detection.bbox)
            self._health.set("depth_ms", self._inflight.depth.get("depth_ms"))
        self.state = WorkerState.ACTIVE
        self._check_active(now)

    def _check_active(self, now: float) -> None:
        f = self._inflight
        assert f is not None
        if f.future.done():
            result, reason, details = normalize(f.future)
            self._finish(f, result, reason, now, details)
            self._inflight = None
            self.state = WorkerState.IDLE
        elif now >= f.deadline:
            self._health.incr("confirmation_timeouts")
            self._finish(f, Result.UNKNOWN, "timeout", now)
            if self._backend.cancel(f.candidate.event_id):
                self._inflight = None
                self.state = WorkerState.IDLE
            else:
                self._draining_since = now
                self.state = WorkerState.DRAINING

    def _check_draining(self, now: float) -> None:
        f = self._inflight
        assert f is not None
        if f.future.done():
            self._health.incr("late_results_ignored")
            self._inflight = None
            self.state = WorkerState.IDLE
        elif now - self._draining_since >= self._cfg.confirm.drain_s:
            self.state = WorkerState.UNAVAILABLE
            self._health.fault("backend", "request did not finish or cancel after timeout")
            for c in self._queue.drain():
                self._skip(c, SkipReason.BACKEND_UNAVAILABLE, now)

    def _finish(self, f: _InFlight, result: Result, reason: str, now: float,
                details: dict | None = None) -> None:
        if result is Result.UNKNOWN:
            self._health.incr(f"unknown_{reason.split(':')[0]}")
        self._health.incr(f"result_{result.value}")
        self._health.set("last_inference_s", now - f.dispatched)
        record = dict(f.record)
        record.update(
            status="complete",
            result=result.value,
            reason=reason,
            inference_duration_s=now - f.dispatched,
            inference_wall_s=time.monotonic() - f.dispatched_host,
            completed_mono=now,
            committed_wall=self._wall(),
            model_io=details,
        )
        if f.depth is not None:
            record.update(distance_m=f.depth.get("distance_m"), depth=f.depth)
        committed = True
        if self._store is not None:
            try:
                self._store.commit_result(f.candidate.event_id, record)
            except StorageError:
                committed = False
        c = f.candidate
        distance = f.depth.get("distance_m") if f.depth else None
        self._emit(Completion(c.key, c.event_id, result, reason, committed, distance))

    def _skip(self, c: Candidate, reason: SkipReason, now: float, detail: str = "") -> None:
        self._health.incr(f"skipped_{reason.value}")
        self._emit(Skip(c.key, c.event_id, reason, now, detail))

    def _record(self, c: Candidate, crop_jpeg: bytes, now: float) -> dict:
        info, det, cfg = c.frame_info, c.detection, self._cfg
        backend = self._backend.info
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "pending",
            "event_id": c.event_id,
            "session_id": c.key.session_id,
            "source_id": info.source_id,
            "source_epoch": c.key.source_epoch,
            "frame_id": info.frame_id,
            "track_id": c.key.track_id,
            "attempt": c.attempt,
            "timestamps": {
                "capture_ts": info.capture_ts,
                "capture_clock": info.capture_clock.value,
                "capture_quality": info.capture_quality,
                "host_arrival_wall": info.arrival_wall,
                "host_arrival_mono": info.arrival_mono,
                "video_pts_s": info.pts_s,
                "track_first_seen_mono": c.track_first_seen_mono,
                "queued_mono": c.queued_mono,
                "dispatched_mono": now,
            },
            "image": {"width": info.width, "height": info.height},
            "bbox": det.bbox.as_list(),
            "crop_region": c.crop_region.as_list(),
            "detector_confidence": det.confidence,
            "class_name": det.class_name,
            "model": {
                "backend": backend.name,
                "model_revision": backend.model_revision,
                "backend_version": backend.backend_version,
                "prompt_sha256": self._prompt_sha256,
            },
            "decision": {
                "min_hits": cfg.gate.min_hits,
                "window_frames": cfg.gate.window_frames,
                "window_s": cfg.gate.window_s,
                "min_confidence": cfg.gate.min_confidence,
                "pad_fraction": cfg.crop.pad_fraction,
                "deadline_s": cfg.confirm.deadline_s,
                "max_unknown_retries": cfg.confirm.max_unknown_retries,
            },
            "files": {
                "crop": "crop.jpg",
                "crop_sha256": hashlib.sha256(crop_jpeg).hexdigest(),
                "frame": "frame.jpg" if c.frame is not None and cfg.storage.save_frame else None,
            },
            "result": None,
            "reason": None,
            "person_score": None,
            "queue_delay_s": now - c.queued_mono,
            "inference_duration_s": None,
            "committed_wall": None,
            "pose": None,
            "attitude": None,
            "distance_m": None,
            "location_status": "unavailable",
            **self._record_extra,
        }
