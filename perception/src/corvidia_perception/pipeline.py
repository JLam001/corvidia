"""Wires the gate, queue, worker, evidence store, and health together."""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Sequence

from .config import PipelineConfig
from .confirm_queue import ConfirmationQueue
from .best_shot import BestShot, BestShotTracker
from .confirmer import ConfirmerBackend
from .evidence import EvidenceStore, encode_jpeg
from .gate import CandidateGate
from .health import Health
from .records import Completion, Detection, Frame, Skip
from .worker import ConfirmationWorker


class LatestFrameSlot:
    """Camera-to-detector buffer holding one pending frame; newer frames replace it."""

    def __init__(self, health: Health | None = None) -> None:
        self._cond = threading.Condition()
        self._frame: Frame | None = None
        self._health = health
        self._ended = False

    def put(self, frame: Frame) -> None:
        with self._cond:
            if self._frame is not None and self._health is not None:
                self._health.incr("frames_superseded")
            self._frame = frame
            self._cond.notify()

    def put_end(self) -> None:
        """No more frames will arrive; wake any waiting reader."""
        with self._cond:
            self._ended = True
            self._cond.notify_all()

    def take(self, timeout: float | None = None) -> Frame | None:
        with self._cond:
            if self._frame is None and not self._ended:
                self._cond.wait(timeout)
            frame, self._frame = self._frame, None
            return frame


class EventPipeline:
    """Detector-side entry point (`on_frame`) plus a background confirmation worker.

    Call `tick()` directly for deterministic tests, or `start()` to run it on a thread.
    """

    def __init__(self, cfg: PipelineConfig, backend: ConfirmerBackend, *,
                 session_id: str | None = None, store: bool = True,
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time,
                 free_bytes: Callable | None = None, record_extra: dict | None = None,
                 depth=None) -> None:
        self.cfg = cfg
        self.clock = clock
        self.session_id = session_id or time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
        self.health = Health()
        self.queue = ConfirmationQueue(cfg.queue.max_pending, cfg.queue.expiry_s)
        self.store: EvidenceStore | None = None
        if store:
            kwargs = {"free_bytes": free_bytes} if free_bytes is not None else {}
            self.store = EvidenceStore(cfg.storage, self.session_id, self.health, **kwargs)
        self._inbox: deque[Completion | Skip] = deque()
        self._skip_log: deque[Skip] = deque()
        self.completions: deque[Completion] = deque(maxlen=256)
        self.worker = ConfirmationWorker(cfg, self.queue, backend, self.store, self.health,
                                         self._from_worker, wall, record_extra, depth, clock)
        self.gate = CandidateGate(cfg, self.queue, self.health, self.session_id, self._accepting)
        self.best = (BestShotTracker(cfg.best_shot, cfg.crop, cfg.gate.min_confidence)
                     if cfg.best_shot.enabled and store else None)
        self._best_out: deque[BestShot] = deque()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._tick_lock = threading.Lock()
        self.interrupted: list = []

    # -- lifecycle ----------------------------------------------------------------------

    def open(self) -> None:
        if self.store is not None:
            self.interrupted = self.store.open()
            self.health.set("interrupted_on_startup", len(self.interrupted))

    def start(self, interval_s: float = 0.005) -> None:
        self.open()
        self._stop.clear()

        def loop() -> None:
            while not self._stop.is_set():
                self.tick()
                self._wake.wait(interval_s)
                self._wake.clear()

        self._thread = threading.Thread(target=loop, daemon=True, name="confirmation-worker")
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        with self._tick_lock:
            self.worker.shutdown(self.clock())
            self._write_skip_log()
            if self.best is not None:
                self._best_out.extend(self.best.flush())
            self._write_best_shots()

    # -- detector thread ----------------------------------------------------------------

    def on_frame(self, frame: Frame, detections: Sequence[Detection]) -> None:
        now = self.clock()
        # Host time, independent of the pipeline clock (which may be video time).
        self.health.set("source_age_s", time.monotonic() - frame.info.arrival_mono)
        self._drain_inbox(now)
        for skip in self.gate.update(frame, detections, now):
            self._log_skip(skip)
        if self.best is not None:
            self.best.update(frame, list(detections), self.gate.tracks, self.session_id)
            self._best_out.extend(self.best.finished(self.gate.tracks))
        self._wake.set()

    def source_lost(self) -> None:
        self.health.incr("source_lost")
        self.gate.source_lost(self.clock())

    # -- worker thread ------------------------------------------------------------------

    def tick(self) -> None:
        with self._tick_lock:
            self.worker.tick(self.clock())
            self._write_skip_log()
            self._write_best_shots()

    # -- internals ----------------------------------------------------------------------

    def _accepting(self) -> bool:
        return self.worker.available and (self.store is None or self.store.accepting())

    def _from_worker(self, item: Completion | Skip) -> None:
        self._inbox.append(item)
        if isinstance(item, Skip):
            self._log_skip(item)
        elif item.committed:
            self.completions.append(item)

    def _drain_inbox(self, now: float) -> None:
        while self._inbox:
            item = self._inbox.popleft()
            if isinstance(item, Completion):
                self.gate.apply_completion(item, now)
            else:
                self.gate.apply_skip(item)

    def _log_skip(self, skip: Skip) -> None:
        if len(self._skip_log) >= self.cfg.storage.outcome_log_max:
            self.health.incr("skip_log_dropped")
            return
        self._skip_log.append(skip)

    def _write_best_shots(self) -> None:
        cfg = self.cfg
        while self._best_out:
            shot = self._best_out.popleft()
            if self.store is None:
                continue
            jpeg = encode_jpeg(shot.crop, cfg.best_shot.jpeg_quality, cfg.crop.color_order)
            if self.store.write_best(shot.event_id, jpeg, shot.meta):
                self.health.incr("best_shots_saved")

    def _write_skip_log(self) -> None:
        while self._skip_log:
            s = self._skip_log.popleft()
            if self.store is not None:
                self.store.append_skip({
                    "event_id": s.event_id,
                    "session_id": s.key.session_id,
                    "source_epoch": s.key.source_epoch,
                    "track_id": s.key.track_id,
                    "reason": s.reason.value,
                    "detail": s.detail,
                    "mono": s.mono,
                })
