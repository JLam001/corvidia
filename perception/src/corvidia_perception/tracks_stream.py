"""Per-frame person tracks as JSON datagrams, for an external mission loop.

Published from the detector thread right after ByteTrack, once per processed frame (or capped
at [tracks] max_hz), to [tracks] udp = "host:port":

    {"t": <wall clock>, "frame_id", "source_epoch", "width", "height", "capture_ts",
     "tracks": [{"id", "x1", "y1", "x2", "y2", "conf", "age", "depth_m"}, ...]}

Boxes are normalized 0..1 (x right, y down). "age" is the number of processed frames in which
this id has been seen in the current source epoch (a source restart resets it). "depth_m" is the
median metric depth over the torso region of the box (central 40 % of the width, 20-60 % of the
height, the same region the per-event distance uses) taken from the free-space stream's newest
map, or -1.0 when no map is available. The field names are the autodrone framework's
PersonTracks contract, so its bridge forwards the datagram unchanged. An empty list is still
published: "no people in this frame" is information the mission loop needs.
"""

from __future__ import annotations

import json
import socket
import time
from collections import deque
from collections.abc import Callable, Sequence

import numpy as np

from .config import TracksConfig
from .health import Health
from .records import Detection, Frame


def torso_median_m(depth_map: np.ndarray, x1: float, y1: float, x2: float, y2: float) -> float:
    """Median metric depth over the torso part of a normalized box, or -1.0 if nothing finite."""
    h, w = depth_map.shape[:2]
    bw, bh = x2 - x1, y2 - y1
    c0 = int(np.floor((x1 + 0.3 * bw) * w))
    c1 = int(np.ceil((x1 + 0.7 * bw) * w))
    r0 = int(np.floor((y1 + 0.2 * bh) * h))
    r1 = int(np.ceil((y1 + 0.6 * bh) * h))
    c0, c1 = max(0, min(w - 1, c0)), min(w, max(c1, c0 + 1))
    r0, r1 = max(0, min(h - 1, r0)), min(h, max(r1, r0 + 1))
    roi = depth_map[r0:r1, c0:c1]
    roi = roi[np.isfinite(roi)]
    return round(float(np.median(roi)), 2) if roi.size else -1.0


class TracksPublisher:
    def __init__(self, cfg: TracksConfig, health: Health,
                 depth_map_source: Callable[[], tuple[np.ndarray, int, int] | None] | None = None) -> None:
        if not cfg.udp:
            raise ValueError("tracks.udp must be host:port")
        host, port = cfg.udp.rsplit(":", 1)
        self._addr = (socket.gethostbyname(host or "127.0.0.1"), int(port))  # resolve once, not per frame
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.cfg, self.health = cfg, health
        self._depth_map_source = depth_map_source
        self._min_period = 1.0 / cfg.max_hz if cfg.max_hz > 0 else 0.0
        self._epoch: int | None = None
        self._ages: dict[int, tuple[int, int]] = {}   # id -> (frames seen, last frame_id)
        self._last_pub = 0.0
        self._pub_times: deque[float] = deque(maxlen=20)
        self.published = 0
        self.udp_errors = 0

    def publish(self, frame: Frame, dets: Sequence[Detection]) -> bool:
        info = frame.info
        if info.source_epoch != self._epoch:
            self._epoch, self._ages = info.source_epoch, {}
        fid = info.frame_id
        for d in dets:
            seen, _ = self._ages.get(d.track_id, (0, fid))
            self._ages[d.track_id] = (seen + 1, fid)
        for tid in [t for t, (_, last) in self._ages.items() if fid - last > 60]:
            del self._ages[tid]  # tracker buffer is 30 frames; anything older is a new encounter
        now = time.monotonic()
        if self._min_period and now - self._last_pub < self._min_period:
            return False
        dmap = self._depth_map_source() if self._depth_map_source is not None else None
        w, h = float(info.width), float(info.height)
        tracks = []
        for d in dets:
            b = d.bbox
            x1, y1 = min(1.0, max(0.0, b.x1 / w)), min(1.0, max(0.0, b.y1 / h))
            x2, y2 = min(1.0, max(0.0, b.x2 / w)), min(1.0, max(0.0, b.y2 / h))
            depth_m = torso_median_m(dmap[0], x1, y1, x2, y2) if dmap is not None else -1.0
            tracks.append({"id": int(d.track_id), "x1": round(x1, 4), "y1": round(y1, 4),
                           "x2": round(x2, 4), "y2": round(y2, 4), "conf": round(float(d.confidence), 3),
                           "age": self._ages[d.track_id][0], "depth_m": depth_m})
        msg = {"t": time.time(), "frame_id": fid, "source_epoch": info.source_epoch,
               "width": info.width, "height": info.height, "capture_ts": info.capture_ts,
               "depth_frame_id": dmap[2] if dmap is not None else None, "tracks": tracks}
        try:
            self._sock.sendto(json.dumps(msg).encode(), self._addr)
        except OSError:
            self.udp_errors += 1
            self.health.incr("tracks_udp_errors")
            return False
        self.published += 1
        self._last_pub = now
        self._pub_times.append(now)
        self.health.incr("tracks_published")
        self.health.set("tracks_count", len(tracks))
        if len(self._pub_times) > 1 and self._pub_times[-1] > self._pub_times[0]:
            self.health.set("tracks_hz", round((len(self._pub_times) - 1) / (self._pub_times[-1] - self._pub_times[0]), 1))
        return True

    def summary(self) -> dict:
        return {"udp": self.cfg.udp, "published": self.published, "udp_errors": self.udp_errors,
                "max_hz": self.cfg.max_hz}

    def close(self) -> None:
        self._sock.close()
