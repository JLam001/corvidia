#!/usr/bin/env python3
"""Corvidia -> autodrone bus bridge.

corvidia-run owns the camera and the GPU models. This process turns its UDP streams and its
evidence store into the topics the autodrone mission loop consumes, so the framework's broker,
fc, control and mission nodes run unchanged beside it (do not start its camera or perception
nodes: they would open the same Argus sensor and run a second detector).

    perc.freespace    <- corvidia-run --freespace-udp    (FreeSpace fields, forwarded as-is)
    perc.tracks       <- corvidia-run --tracks-udp       (PersonTracks fields, forwarded as-is)
    health.perception <- tracks stream alive (free-space staleness is only reported in the detail:
                         the mission's own guard judges free-space age directly)
    health.camera     <- frame ids advancing in the tracks stream, no camera/source/memory faults
                         in Corvidia's /health (optional poll)
    capture.event     <- each completed event.json in Corvidia's newest session (frame, crop,
                         best shot paths; verified from the confirmer result)
    cam.frame         <- optional: JPEG frames from Corvidia's MJPEG preview, so the mission's
                         CAPTURE burst and context frame have something to read. Preview frames
                         carry the drawn boxes; the clean evidence is in capture.event paths.

Run inside the autodrone environment on the same host as corvidia-run:

    python corvidia_bridge.py --profile ~/autodrone/configs/host-jetson.toml
    python -m autodrone.launch --profile ~/autodrone/configs/host-jetson.toml --only broker,fc,control,mission

Profile section (every key optional):

    [corvidia_bridge]
    host = "127.0.0.1"
    freespace_port = 5601
    tracks_port = 5602
    stale_s = 1.0                     # a stream older than this marks its layer unhealthy
    events_root = "~/corvidia-data/events"
    session = ""                      # a session directory; empty = follow the newest
    publish_capture = true
    best_grace_s = 5.0                # wait this long for best.jpg before emitting a capture without it
    health_url = ""                   # e.g. "http://127.0.0.1:8080/health" (corvidia-run --preview-port 8080)
    preview_url = ""                  # e.g. "http://127.0.0.1:8080/stream"  (the MJPEG endpoint) -> cam.frame
    frame_hz = 5.0

Everything above the node class is plain Python with no framework import, so it is unit-tested
anywhere (test_bridge.py); autodrone is imported only when the node starts.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path

log = logging.getLogger("corvidia_bridge")

DEFAULTS = {
    "host": "127.0.0.1", "freespace_port": 5601, "tracks_port": 5602, "stale_s": 1.0,
    "events_root": "~/corvidia-data/events", "session": "", "publish_capture": True,
    "health_url": "", "preview_url": "", "frame_hz": 5.0, "heartbeat_s": 0.5, "scan_s": 1.0,
    "best_grace_s": 5.0,
}
CAMERA_FAULTS = ("camera", "source", "memory_low")


# ---------------------------------------------------------------- UDP in
class UdpLatest:
    """Non-blocking UDP receiver that keeps only the newest JSON datagram."""

    def __init__(self, host: str, port: int) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((host, port))  # a second bridge on the same port must fail here, not steal datagrams
        self.sock.setblocking(False)
        self.received = 0
        self.malformed = 0

    def poll(self) -> dict | None:
        newest = None
        while True:
            try:
                data, _ = self.sock.recvfrom(65536)
            except (BlockingIOError, InterruptedError):
                break
            self.received += 1
            try:
                d = json.loads(data)
                if isinstance(d, dict):
                    newest = d
                else:
                    self.malformed += 1
            except ValueError:
                self.malformed += 1
        return newest

    def close(self) -> None:
        self.sock.close()


class Liveness:
    """When a stream was last heard from, and whether its frame ids keep advancing."""

    def __init__(self) -> None:
        self.last_t: float | None = None
        self.last_frame_id: int | None = None
        self.last_advance_t: float | None = None
        self.epoch: int | None = None
        self._times: deque[float] = deque(maxlen=20)

    def update(self, now: float, frame_id: int | None = None, epoch: int | None = None) -> None:
        self.last_t = now
        self._times.append(now)
        if frame_id is not None:
            if epoch != self.epoch or self.last_frame_id is None or frame_id > self.last_frame_id:
                self.last_advance_t = now
            self.last_frame_id, self.epoch = frame_id, epoch

    def alive(self, now: float, stale_s: float) -> bool:
        return self.last_t is not None and now - self.last_t <= stale_s

    def advancing(self, now: float, stale_s: float) -> bool:
        return self.last_advance_t is not None and now - self.last_advance_t <= stale_s

    @property
    def rate_hz(self) -> float:
        if len(self._times) < 2 or self._times[-1] <= self._times[0]:
            return 0.0
        return (len(self._times) - 1) / (self._times[-1] - self._times[0])


# ---------------------------------------------------------------- datagram -> message dicts
def freespace_msg(d: dict, now: float) -> dict:
    cols = d.get("columns")
    if not isinstance(cols, list) or not cols:
        raise ValueError("freespace datagram without columns")
    return {"t": float(d.get("t") or now), "columns": [float(c) for c in cols],
            "center_free": float(d.get("center_free", 0.0)), "all_close": bool(d.get("all_close", False)),
            "hfov_deg": float(d.get("hfov_deg", 90.0)), "nearest_m": float(d.get("nearest_m", -1.0))}


def tracks_msg(d: dict, now: float) -> dict:
    raw = d.get("tracks")
    if not isinstance(raw, list):
        raise ValueError("tracks datagram without a tracks list")
    tracks = []
    for t in raw:
        tracks.append({"id": int(t["id"]), "x1": float(t["x1"]), "y1": float(t["y1"]), "x2": float(t["x2"]),
                       "y2": float(t["y2"]), "conf": float(t["conf"]), "age": int(t.get("age", 0)),
                       "depth_m": float(t.get("depth_m", -1.0))})
    return {"t": float(d.get("t") or now), "tracks": tracks}


def heartbeat_msgs(now: float, tracks: Liveness, freespace: Liveness | None, corvidia_health: dict | None,
                   stale_s: float) -> list[tuple[str, dict]]:
    """health.perception and health.camera as the mission's LayerHealth expects them."""
    problems = []
    if not tracks.alive(now, stale_s):
        problems.append("no tracks" if tracks.last_t is None else f"tracks stale {now - tracks.last_t:.1f}s")
    notes = []
    if freespace is not None and freespace.last_t is not None and not freespace.alive(now, stale_s):
        notes.append(f"free space stale {now - freespace.last_t:.1f}s")  # diagnostic only: L4 guards it itself
    perception = {"t": now, "layer": "perception", "ok": not problems, "detail": ", ".join(problems + notes),
                  "rate_hz": round(tracks.rate_hz, 1)}
    cam_problems = []
    if not tracks.advancing(now, stale_s):
        cam_problems.append("frames not advancing" if tracks.last_frame_id is not None else "no frames")
    h = corvidia_health or {}
    faults = {**((h.get("source") or {}).get("faults") or {}), **(h.get("faults") or {})}
    for name in CAMERA_FAULTS:
        if name in faults:
            cam_problems.append(f"{name}: {faults[name]}")
    camera = {"t": now, "layer": "camera", "ok": not cam_problems, "detail": ", ".join(cam_problems),
              "rate_hz": round(tracks.rate_hz, 1)}
    return [("health.perception", perception), ("health.camera", camera)]


# ---------------------------------------------------------------- evidence -> capture.event
def capture_from_event(event: dict, event_dir: Path) -> dict:
    paths = [str(event_dir / n) for n in ("frame.jpg", "crop.jpg", "best.jpg") if (event_dir / n).exists()]
    backend = (event.get("model") or {}).get("backend", "")
    result = event.get("result")
    if "stub" in backend:  # "stub" (accuracy replay) or "delayed-stub" (live runs)
        verified = "unverified"
    else:
        verified = {"confirmed": "yes", "rejected": "no"}.get(result, "unknown")
    return {"t": float(event.get("committed_wall") or time.time()), "track_id": int(event.get("track_id", -1)),
            "paths": paths, "verified": verified, "p_person": -1.0, "x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0}


class EventTailer:
    """Emit each completed event.json of the newest Corvidia session once.

    An event is emitted when its best shot exists (best.jpg is written when the track ends,
    usually a second or two after the confirmation) or after `best_grace_s`, whichever is
    first. Events completed before the bridge started are skipped unless a fixed `session`
    is given, so a restart does not replay an earlier run as fresh captures."""

    def __init__(self, root: str, session: str = "", best_grace_s: float = 5.0,
                 started_wall: float | None = None) -> None:
        self.root = Path(os.path.expanduser(root))
        self.fixed = Path(os.path.expanduser(session)) if session else None
        self.best_grace_s = best_grace_s
        self.started_wall = time.time() if started_wall is None else started_wall
        self._session: Path | None = None
        self._session_checked = 0.0
        self._seen: set[Path] = set()
        self._waiting: dict[Path, float] = {}  # complete events waiting for best.jpg

    def session_dir(self, now: float) -> Path | None:
        if self.fixed is not None:
            return self.fixed if self.fixed.is_dir() else None
        if now - self._session_checked >= 5.0 or self._session is None:
            self._session_checked = now
            try:
                dirs = [p for p in self.root.iterdir() if p.is_dir()]
            except OSError:
                dirs = []
            newest = max(dirs, key=lambda p: p.stat().st_mtime, default=None)
            if newest != self._session:
                self._session, self._seen, self._waiting = newest, set(), {}
        return self._session

    def new_events(self, now: float) -> list[dict]:
        session = self.session_dir(now)
        if session is None:
            return []
        out = []
        for path in sorted(session.glob("*/event.json")):
            if path in self._seen:
                continue
            try:
                event = json.loads(path.read_text())
            except (OSError, ValueError):
                continue  # being written; try again next scan
            if event.get("status") != "complete" or event.get("result") is None:
                continue
            committed = event.get("committed_wall")
            if self.fixed is None and committed is not None and committed < self.started_wall - 1.0:
                self._seen.add(path)  # finished before this bridge started: not a fresh capture
                continue
            first = self._waiting.setdefault(path, now)
            if not (path.parent / "best.jpg").exists() and now - first < self.best_grace_s:
                continue  # give the best shot a moment to land
            self._seen.add(path)
            self._waiting.pop(path, None)
            out.append(capture_from_event(event, path.parent))
        return out


# ---------------------------------------------------------------- optional: MJPEG preview -> cam.frame
def jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """(width, height) from the first SOF marker, without decoding."""
    i = 2
    n = len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return (w, h) if w and h else None
        seg = int.from_bytes(data[i + 2:i + 4], "big")
        i += 2 + seg
    return None


def split_jpegs(buf: bytes) -> tuple[list[bytes], bytes]:
    """Complete JPEGs found in a byte buffer, and the unconsumed tail."""
    out = []
    while True:
        start = buf.find(b"\xff\xd8")
        if start < 0:
            return out, b""
        end = buf.find(b"\xff\xd9", start + 2)
        if end < 0:
            return out, buf[start:]
        out.append(buf[start:end + 2])
        buf = buf[end + 2:]


class MjpegLatest:
    def __init__(self, url: str) -> None:
        self.url = url
        self._latest: bytes | None = None
        self.seq = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="mjpeg")

    def start(self) -> None:
        self._thread.start()

    def latest(self) -> tuple[bytes, int] | None:
        with self._lock:
            return (self._latest, self.seq) if self._latest is not None else None

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        buf = b""
        while not self._stop.is_set():
            try:
                with urllib.request.urlopen(self.url, timeout=5) as resp:
                    while not self._stop.is_set():
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        frames, buf = split_jpegs(buf + chunk)
                        if frames:
                            with self._lock:
                                self._latest = frames[-1]
                                self.seq += len(frames)
                log.warning("preview stream ended (is %s the MJPEG endpoint, e.g. .../stream?)", self.url)
            except OSError as e:
                log.warning("preview stream: %s", e)
            buf = b""
            self._stop.wait(1.0)  # never hammer the preview server on EOF or error


class HealthPoller:
    def __init__(self, url: str, period_s: float = 1.0) -> None:
        self.url, self.period = url, period_s
        self._latest: dict | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="corvidia-health")

    def start(self) -> None:
        self._thread.start()

    def latest(self) -> dict | None:
        with self._lock:
            return self._latest

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with urllib.request.urlopen(self.url, timeout=1.0) as resp:
                    d = json.loads(resp.read())
                with self._lock:
                    self._latest = d if isinstance(d, dict) else None
            except (OSError, ValueError):
                with self._lock:
                    self._latest = {"faults": {"source": "corvidia /health unreachable"}}
            self._stop.wait(self.period)


# ---------------------------------------------------------------- the node (framework imported here)
def run_node() -> None:
    from autodrone.msgs import CaptureEvent, Frame, FreeSpace, Heartbeat, PersonTracks
    from autodrone.node import Node, main

    class CorvidiaBridge(Node):
        name = "corvidia_bridge"
        layer = None  # publishes health.camera and health.perception on Corvidia's behalf
        rate_hz = 50.0
        subscribes = ()

        def __init__(self, cfg: dict) -> None:
            super().__init__(cfg)
            c = {**DEFAULTS, **cfg.get("corvidia_bridge", {})}
            self.c = c
            self.fs_in = UdpLatest(c["host"], int(c["freespace_port"]))
            self.tr_in = UdpLatest(c["host"], int(c["tracks_port"]))
            self.fs_live, self.tr_live = Liveness(), Liveness()
            self.tailer = (EventTailer(c["events_root"], c["session"], float(c["best_grace_s"]))
                           if c["publish_capture"] else None)
            self.health_poll = HealthPoller(c["health_url"]) if c["health_url"] else None
            self.mjpeg = MjpegLatest(c["preview_url"]) if c["preview_url"] else None
            self._last_hb = self._last_scan = self._last_frame = 0.0
            self._frame_seq = -1

        def setup(self) -> None:
            if self.health_poll:
                self.health_poll.start()
            if self.mjpeg:
                self.mjpeg.start()
            self.log.info("free space udp :%s, tracks udp :%s, events %s, health %s, preview %s",
                          self.c["freespace_port"], self.c["tracks_port"], self.c["events_root"],
                          self.c["health_url"] or "-", self.c["preview_url"] or "-")

        def tick(self, now: float, inbox: list) -> None:
            d = self.fs_in.poll()
            if d is not None:
                try:
                    self.pub.send("perc.freespace", FreeSpace.from_dict(freespace_msg(d, now)))
                    self.fs_live.update(now)
                except (ValueError, KeyError, TypeError) as e:
                    self.log.warning("bad free-space datagram: %s", e)
            d = self.tr_in.poll()
            if d is not None:
                try:
                    self.pub.send("perc.tracks", PersonTracks.from_dict(tracks_msg(d, now)))
                    self.tr_live.update(now, d.get("frame_id"), d.get("source_epoch"))
                except (ValueError, KeyError, TypeError) as e:
                    self.log.warning("bad tracks datagram: %s", e)
            if now - self._last_hb >= self.c["heartbeat_s"]:
                self._last_hb = now
                health = self.health_poll.latest() if self.health_poll else None
                for topic, hb in heartbeat_msgs(now, self.tr_live, self.fs_live, health, float(self.c["stale_s"])):
                    self.pub.send(topic, Heartbeat.from_dict(hb))
            if self.tailer is not None and now - self._last_scan >= self.c["scan_s"]:
                self._last_scan = now
                for cap in self.tailer.new_events(now):
                    self.log.info("capture.event track %d verified=%s %s", cap["track_id"], cap["verified"], cap["paths"][:1])
                    self.pub.send("capture.event", CaptureEvent.from_dict(cap))
            if self.mjpeg is not None and now - self._last_frame >= 1.0 / float(self.c["frame_hz"]):
                got = self.mjpeg.latest()
                if got is not None and got[1] != self._frame_seq:
                    jpeg, seq = got
                    dims = jpeg_dimensions(jpeg)
                    if dims:
                        self._last_frame, self._frame_seq = now, seq
                        self.pub.send("cam.frame", Frame(t=now, seq=seq, width=dims[0], height=dims[1],
                                                         encoding="jpeg", t_capture=now), jpeg)

        def teardown(self) -> None:
            self.fs_in.close()
            self.tr_in.close()
            if self.health_poll:
                self.health_poll.stop()
            if self.mjpeg:
                self.mjpeg.stop()

    main(CorvidiaBridge)


if __name__ == "__main__":
    run_node()
