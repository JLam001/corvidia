"""Free-space stream thread: fan-out, pacing, non-finite guard, log, gauges, UDP."""

import json
import socket
import time

import numpy as np

from corvidia_perception.config import FreeSpaceConfig
from corvidia_perception.depth_stream import FreeSpaceStream
from corvidia_perception.health import Health
from corvidia_perception.records import ClockDomain, Frame, FrameInfo


class FakeDepth:
    height, width = 294, 518

    def __init__(self, maps):
        self.maps = list(maps)
        self.calls = 0
        self.last_ms = 3.0

    def depth_map(self, bgr):
        m = self.maps[min(self.calls, len(self.maps) - 1)]
        self.calls += 1
        return m


def frame(fid: int, epoch: int = 0) -> Frame:
    now = time.monotonic()
    info = FrameInfo("video:test", epoch, fid, 640, 360, now, time.time(), capture_ts=now,
                     capture_clock=ClockDomain.HOST_MONOTONIC, capture_quality="test")
    return Frame(info, np.zeros((360, 640, 3), np.uint8))


def wait_for(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return False


def test_publishes_once_per_new_frame_and_logs(tmp_path):
    near = np.full((294, 518), 5.0, np.float32)
    near[:, :74] = 1.0  # left sector blocked
    depth = FakeDepth([near])
    h = Health()
    s = FreeSpaceStream(depth, FreeSpaceConfig(hz=200, n_cols=7, d_stop=3.0, d_free=6.0), h,
                        log_path=tmp_path / "freespace.jsonl")
    s.start()
    s.offer(frame(1))
    assert wait_for(lambda: s.publishes == 1)
    time.sleep(0.05)
    assert s.publishes == 1  # the same frame is never re-published
    s.offer(frame(2))
    assert wait_for(lambda: s.publishes == 2)
    s.stop()
    rows = [json.loads(l) for l in (tmp_path / "freespace.jsonl").read_text().splitlines()]
    assert [r["frame_id"] for r in rows] == [1, 2]
    assert len(rows[0]["columns"]) == 7 and rows[0]["columns"][0] == 0.0 and rows[0]["columns"][-1] > 0.6
    assert rows[0]["min_m"] == 1.0 and rows[0]["age_at_publish_ms"] >= 0
    g = h.snapshot()["gauges"]
    assert g["freespace_min_m"] == 1.0 and g["freespace_all_close"] is False
    assert h.snapshot()["counters"]["freespace_publishes"] == 2
    assert s.latest()["frame_id"] == 2
    summ = s.summary()
    assert summ["publishes"] == 2 and summ["params"]["n_cols"] == 7 and summ["min_m"]["p50"] == 1.0


def test_nonfinite_map_is_skipped_and_never_poisons_smoothing(tmp_path):
    good = np.full((294, 518), 4.0, np.float32)
    bad = np.full((294, 518), np.nan, np.float32)
    partial = good.copy()
    partial[:10, :] = np.inf  # unknown pixels count as far, not as an obstacle
    depth = FakeDepth([bad, good, partial])
    h = Health()
    s = FreeSpaceStream(depth, FreeSpaceConfig(hz=200), h)
    s.start()
    s.offer(frame(1))
    assert wait_for(lambda: depth.calls >= 1)
    s.offer(frame(2))
    assert wait_for(lambda: s.publishes == 1)
    s.offer(frame(3))
    assert wait_for(lambda: s.publishes == 2)
    s.stop()
    assert s.skipped_nonfinite == 1
    latest = s.latest()
    assert all(np.isfinite(latest["columns"])) and latest["min_m"] == 4.0
    assert h.snapshot()["counters"]["freespace_skipped_nonfinite"] == 1


def test_udp_datagram_carries_the_wire_message(tmp_path):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(2.0)
    port = sock.getsockname()[1]
    depth = FakeDepth([np.full((294, 518), 2.0, np.float32)])
    s = FreeSpaceStream(depth, FreeSpaceConfig(hz=100, udp=f"127.0.0.1:{port}"), Health())
    s.start()
    s.offer(frame(1))
    data, _ = sock.recvfrom(65536)
    s.stop()
    msg = json.loads(data)
    assert set(msg) == {"t", "columns", "center_free", "all_close", "hfov_deg", "nearest_m"}
    assert msg["all_close"] is True and msg["nearest_m"] == 2.0 and msg["t"] > 0


def test_depth_failure_is_a_fault_not_a_crash():
    class Broken(FakeDepth):
        def depth_map(self, bgr):
            raise RuntimeError("engine gone")

    h = Health()
    s = FreeSpaceStream(Broken([]), FreeSpaceConfig(hz=50), h)
    s.start()
    s.offer(frame(1))
    assert wait_for(lambda: "freespace" in h.snapshot()["faults"])
    s.stop()
    assert s.publishes == 0


def test_smoothing_resets_on_source_epoch_change():
    near = np.full((294, 518), 1.0, np.float32)
    far = np.full((294, 518), 6.0, np.float32)
    depth = FakeDepth([near, far])
    s = FreeSpaceStream(depth, FreeSpaceConfig(hz=200, smooth_alpha=0.5, d_stop=3.0, d_free=6.0), Health())
    s.start()
    s.offer(frame(1, epoch=0))
    assert wait_for(lambda: s.publishes == 1)
    s.offer(frame(1, epoch=1))  # camera reconnected: same frame id, new epoch
    assert wait_for(lambda: s.publishes == 2)
    s.stop()
    assert s.latest()["min_m"] == 6.0  # not blended with the 1.0 m map from before the restart


def test_bad_config_or_frame_becomes_a_fault_not_a_dead_thread():
    import pytest

    depth = FakeDepth([np.full((294, 518), 4.0, np.float32)])
    with pytest.raises(ValueError):
        FreeSpaceStream(depth, FreeSpaceConfig(hz=10, n_cols=600), Health())
    h = Health()
    s = FreeSpaceStream(depth, FreeSpaceConfig(hz=200), h)
    s.start()
    s.offer(Frame(frame(1).info, "not an image"))  # depth_map on a fake ignores it; force a later failure
    depth.depth_map = lambda bgr: np.full((294, 518), 4.0, np.float32)[:, :3]  # too narrow for 7 sectors
    s.offer(frame(2))
    assert wait_for(lambda: "freespace" in h.snapshot()["faults"])
    assert s._thread.is_alive()
    s.stop()


def test_stop_before_start_is_safe():
    s = FreeSpaceStream(FakeDepth([np.zeros((294, 518), np.float32)]), FreeSpaceConfig(hz=10, udp="127.0.0.1:1"), Health())
    s.stop()
    s.stop()
