"""Evidence storage with atomic commits, storage limits, and crash recovery.

Layout:
    <root>/<session_id>/<event_id>/{frame.jpg, crop.jpg, event.json}
    <root>/<session_id>/skips.jsonl

`event.json` is written with status "pending" before dispatch and replaced with
status "complete" afterwards. A pending record found at startup is marked
"interrupted"; it is never reported as a confirmation.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import threading
import time
from pathlib import Path

import numpy as np
from PIL import Image

from .config import StorageConfig
from .health import Health

TMP_PREFIX = ".tmp-"


class StorageError(Exception):
    pass


def encode_jpeg(array: np.ndarray, quality: int, color_order: str = "bgr") -> bytes:
    if array.ndim == 3 and array.shape[2] == 3 and color_order == "bgr":
        array = array[:, :, ::-1]
    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(array)).save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, data: bytes) -> None:
    """Write via a temporary file in the same directory, then rename."""
    tmp = path.with_name(f"{TMP_PREFIX}{path.name}")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


class EvidenceStore:
    """Called from the confirmation worker thread; `accepting()` from any thread."""

    def __init__(self, cfg: StorageConfig, session_id: str, health: Health,
                 free_bytes=lambda p: shutil.disk_usage(p).free) -> None:
        self._cfg = cfg
        self._health = health
        self._free_bytes = free_bytes
        self.session_dir = cfg.root / session_id
        self._lock = threading.Lock()
        self._bytes_used = 0
        self._accepting = True
        self._faulted = False
        self._last_check = -1.0

    def open(self) -> list[Path]:
        """Create the session directory, recover interrupted events, and measure usage."""
        self.session_dir.mkdir(parents=True, exist_ok=True)
        interrupted = self.recover_interrupted()
        self._bytes_used = sum(
            p.stat().st_size for p in self._cfg.root.rglob("*") if p.is_file()
        )
        self._refresh(force=True)
        return interrupted

    # -- admission ----------------------------------------------------------------------

    def accepting(self) -> bool:
        self._refresh()
        return self._accepting

    def _refresh(self, force: bool = False) -> None:
        now = time.monotonic()
        with self._lock:
            if not force and now - self._last_check < 1.0:
                return
            self._last_check = now
            used = self._bytes_used
        try:
            free = self._free_bytes(self._cfg.root)
        except OSError as e:
            self._set_fault(f"disk_usage failed: {e}")
            return
        ok = used < self._cfg.max_bytes and free >= self._cfg.min_free_bytes
        with self._lock:
            self._accepting = ok and not self._faulted
        self._health.set("storage_bytes_used", used)
        self._health.set("storage_free_bytes", free)
        if not ok:
            self._health.fault("storage_limit", f"used={used} free={free}")
        else:
            self._health.clear_fault("storage_limit")

    def _set_fault(self, detail: str) -> None:
        with self._lock:
            self._faulted = True
            self._accepting = False
        self._health.incr("storage_errors")
        self._health.fault("storage", detail)

    def clear_fault(self) -> None:
        """Operator acknowledgement after fixing the storage problem."""
        with self._lock:
            self._faulted = False
        self._health.clear_fault("storage")
        self._refresh(force=True)

    # -- writes -------------------------------------------------------------------------

    def _write(self, path: Path, data: bytes) -> None:
        atomic_write(path, data)
        with self._lock:
            self._bytes_used += len(data)

    def commit_candidate(self, event_id: str, crop_jpeg: bytes, frame_jpeg: bytes | None,
                         record: dict) -> None:
        """Commit evidence before dispatch. Raises StorageError on any failure."""
        if not self.accepting():
            raise StorageError("storage limit reached")
        event_dir = self.session_dir / event_id
        try:
            event_dir.mkdir(parents=False, exist_ok=False)
            _fsync_dir(self.session_dir)
            if frame_jpeg is not None:
                self._write(event_dir / "frame.jpg", frame_jpeg)
            self._write(event_dir / "crop.jpg", crop_jpeg)
            self._write(event_dir / "event.json", _dumps(record))
        except OSError as e:
            self._set_fault(f"commit_candidate {event_id}: {e}")
            raise StorageError(str(e)) from e

    def commit_result(self, event_id: str, record: dict) -> None:
        try:
            self._write(self.session_dir / event_id / "event.json", _dumps(record))
        except OSError as e:
            self._set_fault(f"commit_result {event_id}: {e}")
            raise StorageError(str(e)) from e

    def append_skip(self, record: dict) -> None:
        try:
            with open(self.session_dir / "skips.jsonl", "ab") as f:
                line = json.dumps(record, sort_keys=True).encode() + b"\n"
                f.write(line)
            with self._lock:
                self._bytes_used += len(line)
        except OSError as e:
            self._set_fault(f"append_skip: {e}")

    # -- recovery -----------------------------------------------------------------------

    def recover_interrupted(self) -> list[Path]:
        """Mark any event left pending (or missing its record) as interrupted."""
        recovered: list[Path] = []
        if not self._cfg.root.exists():
            return recovered
        for tmp in self._cfg.root.glob(f"*/*/{TMP_PREFIX}*"):
            tmp.unlink(missing_ok=True)
        for event_dir in self._cfg.root.glob("*/*"):
            if not event_dir.is_dir():
                continue
            path = event_dir / "event.json"
            record: dict
            try:
                record = json.loads(path.read_bytes())
            except (OSError, json.JSONDecodeError):
                record = {"event_id": event_dir.name, "status": "pending"}
            if record.get("status") != "pending":
                continue
            record["status"] = "interrupted"
            record["result"] = None
            record["reason"] = "process_stopped_before_result"
            atomic_write(path, _dumps(record))
            recovered.append(event_dir)
        return recovered


def _dumps(record: dict) -> bytes:
    return json.dumps(record, indent=2, sort_keys=True).encode() + b"\n"
