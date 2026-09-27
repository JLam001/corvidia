"""Bridge logic without the bus: datagram parsing, liveness and heartbeats, event tailing, MJPEG splitting."""

import json
import socket
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import corvidia_bridge as cb  # noqa: E402


def test_udp_latest_keeps_the_newest_and_counts_malformed():
    rx = cb.UdpLatest("127.0.0.1", 0)
    port = rx.sock.getsockname()[1]
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for payload in (b'{"n": 1}', b"not json", b'{"n": 2}', b"[1, 2]"):
        tx.sendto(payload, ("127.0.0.1", port))
    time.sleep(0.05)
    assert rx.poll() == {"n": 2}
    assert rx.poll() is None
    assert rx.received == 4 and rx.malformed == 2
    rx.close()


def test_datagram_conversion_matches_message_fields():
    fs = cb.freespace_msg({"t": 5.0, "columns": [0, 0.5, 1], "center_free": 0.5, "all_close": 0,
                           "hfov_deg": 65, "nearest_m": 2.5}, now=9.0)
    assert fs == {"t": 5.0, "columns": [0.0, 0.5, 1.0], "center_free": 0.5, "all_close": False,
                  "hfov_deg": 65.0, "nearest_m": 2.5}
    tr = cb.tracks_msg({"frame_id": 3, "tracks": [{"id": 4, "x1": 0.1, "y1": 0.2, "x2": 0.3, "y2": 0.9, "conf": 0.8}]}, now=9.0)
    assert tr["t"] == 9.0 and tr["tracks"] == [{"id": 4, "x1": 0.1, "y1": 0.2, "x2": 0.3, "y2": 0.9, "conf": 0.8,
                                                "age": 0, "depth_m": -1.0}]
    with pytest.raises(ValueError):
        cb.freespace_msg({"columns": []}, now=1.0)
    with pytest.raises(ValueError):
        cb.tracks_msg({"tracks": "nope"}, now=1.0)


def test_heartbeats_follow_stream_liveness_and_corvidia_faults():
    tr, fs = cb.Liveness(), cb.Liveness()
    hb = dict(cb.heartbeat_msgs(10.0, tr, fs, None, 1.0))
    assert hb["health.perception"]["ok"] is False and "no tracks" in hb["health.perception"]["detail"]
    assert hb["health.camera"]["ok"] is False and hb["health.camera"]["detail"] == "no frames"
    tr.update(10.0, frame_id=1, epoch=0)
    tr.update(10.5, frame_id=2, epoch=0)
    fs.update(10.4)
    hb = dict(cb.heartbeat_msgs(10.6, tr, fs, None, 1.0))
    assert hb["health.perception"]["ok"] and hb["health.camera"]["ok"]
    assert hb["health.perception"]["rate_hz"] == 2.0
    tr.update(12.0, frame_id=2, epoch=0)  # tracks keep coming but the frame id is stuck
    hb = dict(cb.heartbeat_msgs(12.2, tr, fs, None, 1.0))
    assert hb["health.perception"]["ok"] is True  # tracks alive; free-space staleness is the guard's business
    assert "free space stale" in hb["health.perception"]["detail"]
    assert hb["health.camera"]["ok"] is False and "not advancing" in hb["health.camera"]["detail"]
    tr.update(12.3, frame_id=1, epoch=1)  # camera reconnect: new epoch counts as advancing
    hb = dict(cb.heartbeat_msgs(12.4, tr, None, {"faults": {"memory_low": "1200 MB"}}, 1.0))
    assert hb["health.perception"]["ok"] and hb["health.camera"]["ok"] is False
    assert "memory_low" in hb["health.camera"]["detail"]


def _event(session: Path, eid: str, track: int, result, status="complete", backend="llama.cpp", best=True,
           committed=None):
    d = session / eid
    d.mkdir(parents=True)
    for n in ("frame.jpg", "crop.jpg") + (("best.jpg",) if best else ()):
        (d / n).write_bytes(b"\xff\xd8\xff\xd9")
    (d / "event.json").write_text(json.dumps({"event_id": eid, "track_id": track, "status": status, "result": result,
                                              "model": {"backend": backend},
                                              "committed_wall": time.time() if committed is None else committed}))
    return d


def test_event_tailer_emits_completed_events_once_and_follows_the_newest_session(tmp_path):
    root = tmp_path / "events"
    old = root / "20260927T010000-aaaa"
    _event(old, "e0", 9, "confirmed", committed=time.time() - 3600)  # finished an hour before the bridge started
    _event(old, "e1", 1, "confirmed")
    tailer = cb.EventTailer(str(root), best_grace_s=5.0)
    caps = tailer.new_events(now=100.0)
    assert [c["track_id"] for c in caps] == [1] and caps[0]["verified"] == "yes"
    assert caps[0]["paths"] == [str(old / "e1" / n) for n in ("frame.jpg", "crop.jpg", "best.jpg")]
    assert tailer.new_events(now=100.5) == []  # not emitted twice, and the old one never
    _event(old, "e2", 2, None, status="pending")
    assert tailer.new_events(now=101.0) == []  # pending: not yet
    (old / "e2" / "event.json").write_text(json.dumps({"track_id": 2, "status": "complete", "result": "rejected",
                                                       "model": {"backend": "llama.cpp"}, "committed_wall": time.time()}))
    assert [c["verified"] for c in tailer.new_events(now=102.0)] == ["no"]
    time.sleep(0.02)
    new = root / "20260927T020000-bbbb"
    _event(new, "e1", 5, "confirmed", backend="delayed-stub", best=False)
    assert tailer.new_events(now=110.0) == []  # newest session picked up; waiting for best.jpg
    (new / "e1" / "best.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    caps = tailer.new_events(now=111.0)
    assert [c["track_id"] for c in caps] == [5] and caps[0]["verified"] == "unverified"
    assert len(caps[0]["paths"]) == 3
    _event(new, "e2", 6, "confirmed", best=False)
    assert tailer.new_events(now=112.0) == []
    caps = tailer.new_events(now=117.5)  # grace expired: emitted without the best shot
    assert [c["track_id"] for c in caps] == [6] and len(caps[0]["paths"]) == 2


def test_fixed_session_replays_everything_and_udp_port_conflict_is_loud(tmp_path):
    s = tmp_path / "events" / "20260927T030000-cccc"
    _event(s, "e1", 3, "confirmed", committed=time.time() - 3600)
    caps = cb.EventTailer(str(tmp_path / "events"), session=str(s)).new_events(now=1.0)
    assert [c["track_id"] for c in caps] == [3]
    rx = cb.UdpLatest("127.0.0.1", 0)
    port = rx.sock.getsockname()[1]
    with pytest.raises(OSError):
        cb.UdpLatest("127.0.0.1", port)
    rx.close()


def test_camera_faults_are_read_from_the_nested_source_health():
    tr = cb.Liveness()
    tr.update(5.0, frame_id=1, epoch=0)
    hb = dict(cb.heartbeat_msgs(5.1, tr, None, {"faults": {}, "source": {"faults": {"camera": "read failed"}}}, 1.0))
    assert hb["health.camera"]["ok"] is False and "camera: read failed" in hb["health.camera"]["detail"]


def _fake_jpeg(w: int, h: int) -> bytes:
    sof = b"\xff\xc0" + (17).to_bytes(2, "big") + b"\x08" + h.to_bytes(2, "big") + w.to_bytes(2, "big") + b"\x03" + b"\x00" * 9
    app0 = b"\xff\xe0" + (16).to_bytes(2, "big") + b"JFIF\x00" + b"\x00" * 9
    return b"\xff\xd8" + app0 + sof + b"\xff\xda" + (8).to_bytes(2, "big") + b"\x00" * 6 + b"\x12\x34" + b"\xff\xd9"


def test_jpeg_dimensions_and_mjpeg_splitting():
    a, b = _fake_jpeg(1280, 720), _fake_jpeg(640, 480)
    assert cb.jpeg_dimensions(a) == (1280, 720)
    frames, tail = cb.split_jpegs(b"--boundary\r\nContent-Type: image/jpeg\r\n\r\n" + a + b"\r\n--boundary\r\n" + b[:10])
    assert frames == [a] and tail == b[:10]
    frames, tail = cb.split_jpegs(tail + b[10:])
    assert frames == [b] and tail == b""
    assert cb.jpeg_dimensions(b"\xff\xd8\xff\xd9") is None
