"""Free-space stream: the depth engine on its own thread at a fixed target rate.

Runs beside the detector, not after it: the run loop hands every frame to `offer()` and the
thread always works on the newest one (a one-frame fan-out; frame images are per-frame copies,
so read-only sharing is safe). Each map becomes a sector profile (freespace.py) that is
    - appended to <session>/freespace.jsonl,
    - exposed as health gauges (freespace_*; visible in /health and timeseries.jsonl) and as
      `latest()` for an in-process consumer,
    - optionally sent as a JSON datagram to `udp` ("host:port"), one FreeSpace per profile.

Profiles are stamped at publish time (`t`, wall clock). The map's own age at publish
(publish - frame arrival) is logged as `age_at_publish_ms`: on the Orin Nano's TensorRT engine
this is ~10 ms capture-to-Python + ~46 ms depth idle, more while Cosmos runs. Consumers with a
freshness rule (the autodrone layers use 0.15 s) should budget for it separately.

The engine is shared with the confirmation worker's per-event distance through the lock inside
DepthEstimator.depth_map; the two take turns.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

from .config import FreeSpaceConfig
from .freespace import free_space_from_nearest, sector_nearest
from .health import Health


class FreeSpaceStream:
    def __init__(self, depth, cfg: FreeSpaceConfig, health: Health, log_path: Path | None = None,
                 scale: float = 1.0) -> None:
        if cfg.hz <= 0:
            raise ValueError("freespace.hz must be > 0")
        if not 1 <= cfg.n_cols <= depth.width:
            raise ValueError(f"freespace.n_cols must be between 1 and the depth width ({depth.width})")
        self.depth, self.cfg, self.health = depth, cfg, health
        self.period = 1.0 / cfg.hz
        self.scale = scale
        self._log_path = log_path
        self._latest_frame = None
        self._latest_profile: dict | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="freespace")
        self._started = False
        self._sock = None
        self._addr = None
        if cfg.udp:
            host, port = cfg.udp.rsplit(":", 1)
            self._addr = (host or "127.0.0.1", int(port))
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.publishes = 0
        self.skipped_nonfinite = 0
        self._depth_ms: deque[float] = deque(maxlen=100_000)
        self._age_ms: deque[float] = deque(maxlen=100_000)
        self._min_m: deque[float] = deque(maxlen=100_000)
        self._center: deque[float] = deque(maxlen=100_000)
        self._all_close = 0
        self._pub_times: deque[float] = deque(maxlen=20)
        self._first_pub: float | None = None
        self._last_pub: float | None = None
        self._log = None

    # -- run-loop side --------------------------------------------------------------------
    def offer(self, frame) -> None:
        with self._lock:
            self._latest_frame = frame

    def latest(self) -> dict | None:
        with self._lock:
            return self._latest_profile

    def start(self) -> None:
        if self._log_path is not None:
            try:
                self._log = open(self._log_path, "a")
            except OSError as e:
                self.health.fault("freespace_log", str(e))
                self._log = None
        self._started = True
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._started:
            self._thread.join(timeout)
        if self._started and self._thread.is_alive():
            # Still inside the engine (a stuck execute): leave the file and socket to the daemon
            # thread rather than closing them under it; the process exit reclaims both.
            self.health.fault("freespace", "thread did not stop within the timeout")
            return
        if self._log is not None:
            self._log.close()
            self._log = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def summary(self) -> dict:
        from .run import pct  # local import: run.py imports this module

        span = (self._last_pub - self._first_pub) if self._first_pub is not None and self.publishes > 1 else None
        return {
            "target_hz": self.cfg.hz,
            "achieved_hz": round((self.publishes - 1) / span, 2) if span else None,
            "publishes": self.publishes,
            "skipped_nonfinite": self.skipped_nonfinite,
            "depth_ms": pct(self._depth_ms),
            "age_at_publish_ms": pct(self._age_ms),
            "min_m": pct(self._min_m),
            "center_free": pct(self._center),
            "all_close_fraction": round(self._all_close / self.publishes, 3) if self.publishes else None,
            "params": {"n_cols": self.cfg.n_cols, "near_percentile": self.cfg.near_percentile,
                       "band": list(self.cfg.band), "hfov_deg": self.cfg.hfov_deg, "d_stop": self.cfg.d_stop,
                       "d_free": self.cfg.d_free, "smooth_alpha": self.cfg.smooth_alpha,
                       "depth_input": [self.depth.height, self.depth.width], "scale": self.scale},
        }

    # -- thread ---------------------------------------------------------------------------
    def _run(self) -> None:
        cfg = self.cfg
        last_id = None
        last_epoch = None
        nearest_ema = None
        next_t = time.monotonic()
        while not self._stop.is_set():
            try:
                with self._lock:
                    frame = self._latest_frame
                if frame is None or (frame.info.source_epoch, frame.info.frame_id) == last_id:
                    self._stop.wait(0.005)
                    continue
                last_id = (frame.info.source_epoch, frame.info.frame_id)
                if frame.info.source_epoch != last_epoch or (
                        self._last_pub is not None and time.monotonic() - self._last_pub > 5 * self.period):
                    nearest_ema = None  # source restarted or the stream stalled: do not blend a stale map
                    last_epoch = frame.info.source_epoch
                try:
                    dmap = self.depth.depth_map(frame.image) * self.scale
                    depth_ms = self.depth.last_ms  # read right after our own call, before any other work
                except Exception as e:  # noqa: BLE001 - the stream must never take the run down
                    self.health.fault("freespace", f"{type(e).__name__}: {e}")
                    self._stop.wait(self.period)
                    continue
                finite = np.isfinite(dmap)
                if not finite.any():
                    self.skipped_nonfinite += 1
                    self.health.incr("freespace_skipped_nonfinite")
                    continue  # never publish or smooth a map with no finite values
                if not finite.all():
                    dmap = np.where(finite, dmap, dmap[finite].max())  # unknown = far
                nearest = sector_nearest(dmap, cfg.n_cols, cfg.band, cfg.near_percentile)
                nearest_ema = nearest if nearest_ema is None else (
                    cfg.smooth_alpha * nearest + (1 - cfg.smooth_alpha) * nearest_ema)
                t_pub = time.monotonic()
                fs = free_space_from_nearest(nearest_ema, cfg.d_stop, cfg.d_free, cfg.hfov_deg, t=time.time())
                self._publish(fs, frame, nearest, t_pub, depth_ms)
                self.health.clear_fault("freespace")
                next_t = max(next_t + self.period, t_pub)  # never burst; if late, run again at once
                self._stop.wait(max(0.0, next_t - time.monotonic()))
            except Exception as e:  # noqa: BLE001 - a bad frame or config must not kill the thread silently
                self.health.fault("freespace", f"{type(e).__name__}: {e}")
                self._stop.wait(self.period)

    def _publish(self, fs, frame, nearest, t_pub: float, depth_ms: float) -> None:
        info = frame.info
        age_ms = (t_pub - info.arrival_mono) * 1000
        row = {
            "t": fs.t, "t_pub_mono": t_pub, "source_epoch": info.source_epoch, "frame_id": info.frame_id,
            "capture_ts": info.capture_ts, "capture_quality": info.capture_quality,
            "age_at_publish_ms": round(age_ms, 1), "depth_ms": round(depth_ms, 1),
            "nearest_m": [round(float(x), 2) for x in nearest], "columns": [round(c, 3) for c in fs.columns],
            "center_free": round(fs.center_free, 3), "all_close": fs.all_close, "min_m": round(fs.nearest_m, 2),
        }
        self.publishes += 1
        self._first_pub = self._first_pub if self._first_pub is not None else t_pub
        self._last_pub = t_pub
        self._pub_times.append(t_pub)
        self._depth_ms.append(depth_ms)
        self._age_ms.append(age_ms)
        self._min_m.append(fs.nearest_m)
        self._center.append(fs.center_free)
        self._all_close += int(fs.all_close)
        hz = ((len(self._pub_times) - 1) / (self._pub_times[-1] - self._pub_times[0])
              if len(self._pub_times) > 1 and self._pub_times[-1] > self._pub_times[0] else None)
        with self._lock:
            self._latest_profile = row
        h = self.health
        h.incr("freespace_publishes")
        h.set("freespace_hz", round(hz, 1) if hz else None)
        h.set("freespace_age_ms", round(age_ms, 1))
        h.set("freespace_depth_ms", round(depth_ms, 1))
        h.set("freespace_min_m", round(fs.nearest_m, 2))
        h.set("freespace_center_free", round(fs.center_free, 3))
        h.set("freespace_all_close", fs.all_close)
        if self._log is not None:
            try:
                self._log.write(json.dumps(row) + "\n")
                if self.publishes % 10 == 0:
                    self._log.flush()
            except (OSError, ValueError) as e:
                h.fault("freespace_log", str(e))
                self._log = None
        if self._sock is not None:
            try:
                self._sock.sendto(json.dumps(fs.to_dict()).encode(), self._addr)
            except OSError:
                h.incr("freespace_udp_errors")
