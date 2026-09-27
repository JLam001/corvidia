"""Per-frame tracks datagrams: normalization, age, depth from the latest map, epoch reset, rate cap."""

import json
import socket
import time

import numpy as np

from corvidia_perception.config import TracksConfig
from corvidia_perception.health import Health
from corvidia_perception.records import BBox, ClockDomain, Detection, Frame, FrameInfo
from corvidia_perception.tracks_stream import TracksPublisher, torso_median_m


def frame(fid: int, epoch: int = 0, w: int = 640, h: int = 480) -> Frame:
    now = time.monotonic()
    return Frame(FrameInfo("video:t", epoch, fid, w, h, now, time.time(), capture_ts=now,
                           capture_clock=ClockDomain.HOST_MONOTONIC, capture_quality="test"),
                 np.zeros((h, w, 3), np.uint8))


def receiver():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(2.0)
    return sock, sock.getsockname()[1]


def test_datagram_matches_the_persontracks_contract():
    sock, port = receiver()
    dmap = np.full((294, 518), 6.0, np.float32)
    dmap[:, 259:] = 2.0  # right half of the map is close
    pub = TracksPublisher(TracksConfig(udp=f"127.0.0.1:{port}"), Health(), depth_map_source=lambda: (dmap, 0, 1))
    det = Detection(7, BBox(400, 100, 560, 420), 0.83)  # right side of a 640x480 frame
    assert pub.publish(frame(1), [det])
    assert pub.publish(frame(2), [det])
    a = json.loads(sock.recv(65536))
    b = json.loads(sock.recv(65536))
    pub.close()
    t = a["tracks"][0]
    assert set(t) == {"id", "x1", "y1", "x2", "y2", "conf", "age", "depth_m"}
    assert t["id"] == 7 and t["conf"] == 0.83 and t["age"] == 1
    assert (t["x1"], t["y1"], t["x2"], t["y2"]) == (0.625, 0.2083, 0.875, 0.875)
    assert t["depth_m"] == 2.0  # torso region falls in the close half
    assert b["tracks"][0]["age"] == 2 and b["frame_id"] == 2 and b["depth_frame_id"] == 1
    assert a["width"] == 640 and a["height"] == 480 and a["t"] > 0


def test_empty_frame_still_publishes_and_epoch_resets_age():
    sock, port = receiver()
    h = Health()
    pub = TracksPublisher(TracksConfig(udp=f"127.0.0.1:{port}"), h)
    det = Detection(3, BBox(10, 10, 50, 120), 0.5)
    pub.publish(frame(1), [det])
    pub.publish(frame(2), [])
    pub.publish(frame(1, epoch=1), [det])
    msgs = [json.loads(sock.recv(65536)) for _ in range(3)]
    pub.close()
    assert msgs[1]["tracks"] == [] and msgs[1]["frame_id"] == 2
    assert msgs[2]["tracks"][0]["age"] == 1 and msgs[2]["source_epoch"] == 1
    assert msgs[0]["tracks"][0]["depth_m"] == -1.0  # no map source
    assert h.snapshot()["counters"]["tracks_published"] == 3 and h.snapshot()["gauges"]["tracks_count"] == 1


def test_rate_cap_and_box_clipping():
    sock, port = receiver()
    pub = TracksPublisher(TracksConfig(udp=f"127.0.0.1:{port}", max_hz=5), Health())
    det = Detection(1, BBox(-20, -5, 700, 500), 0.9)  # partly outside the frame
    assert pub.publish(frame(1), [det])
    assert not pub.publish(frame(2), [det])  # within 200 ms of the previous publish
    m = json.loads(sock.recv(65536))
    pub.close()
    t = m["tracks"][0]
    assert (t["x1"], t["y1"], t["x2"], t["y2"]) == (0.0, 0.0, 1.0, 1.0)
    assert pub.summary()["published"] == 1


def test_torso_median_ignores_background_and_nonfinite():
    dmap = np.full((100, 100), 9.0, np.float32)
    dmap[20:60, 30:70] = 3.0
    dmap[25, 40] = np.nan
    assert torso_median_m(dmap, 0.0, 0.0, 1.0, 1.0) == 3.0   # torso region 30-70 % x, 20-60 % y
    assert torso_median_m(np.full((10, 10), np.nan, np.float32), 0.2, 0.2, 0.4, 0.6) == -1.0
