"""Depth: torso-region distance, records, and the TensorRT engine."""

from pathlib import Path

import numpy as np
import pytest

from corvidia_perception.config import DepthConfig
from corvidia_perception.confirmer import StubBackend
from corvidia_perception.depth import DepthEstimator
from corvidia_perception.records import BBox

from conftest import person

ENGINE = Path("~/models/depth/da2-metric-indoor-small-294x518.engine").expanduser()


def fake_estimator(scale=1.0, height=294, width=518):
    est = DepthEstimator.__new__(DepthEstimator)
    est.cfg = DepthConfig(scale=scale)
    est.height, est.width = height, width
    est.model_path = "fake"
    est.last_ms = 1.0
    return est


def test_distance_uses_torso_region_not_background():
    est = fake_estimator()
    depth = np.full((294, 518), 9.0, np.float32)       # background far away
    # Person box in a 1036x588 frame (2x the depth map): x 200-300, y 100-500.
    # Torso region (30-70% width, 20-60% height) -> depth x 115-135, y 90-170.
    depth[90:170, 115:135] = 2.0
    r = est.person_distance(depth, BBox(200, 100, 300, 500), 1036, 588)
    assert r["distance_m"] == 2.0 and r["roi_p75_m"] == 2.0


def test_scale_is_applied():
    est = fake_estimator(scale=1.25)
    depth = np.full((294, 518), 4.0, np.float32)
    assert est.person_distance(depth, BBox(0, 0, 100, 200), 518, 294)["distance_m"] == 5.0


class StubDepth:
    calls = 0

    def measure(self, frame, bbox):
        StubDepth.calls += 1
        return {"distance_m": 3.4, "status": "uncalibrated", "depth_ms": 12.0}


def test_distance_is_written_to_event_and_completion(harness):
    h = harness()
    h.pipe.worker._depth = StubDepth()
    h.frames(3, person(1))
    (event,) = h.events()
    assert event["distance_m"] == 3.4
    assert event["depth"]["status"] == "uncalibrated"
    assert h.pipe.completions[-1].distance_m == 3.4


def test_depth_failure_never_blocks_the_event(harness):
    class Broken:
        def measure(self, frame, bbox):
            return {"distance_m": None, "error": "boom"}

    h = harness()
    h.pipe.worker._depth = Broken()
    h.frames(3, person(1))
    (event,) = h.events()
    assert event["result"] == "confirmed" and event["distance_m"] is None


@pytest.mark.gpu
@pytest.mark.skipif(not ENGINE.exists(), reason="needs the depth engine")
def test_depth_engine_gives_plausible_indoor_distances():
    cv2 = pytest.importorskip("cv2")
    est = DepthEstimator(DepthConfig(model=str(ENGINE)))
    cap = cv2.VideoCapture("/usr/share/opencv4/samples/data/vtest.avi")
    ok, frame = cap.read()
    depth = est.depth_map(frame)
    assert depth.shape == (est.height, est.width)
    assert np.isfinite(depth).all() and 0 < float(np.median(depth)) < 20
    r = est.measure(frame, BBox(250, 220, 285, 310))   # a pedestrian in vtest
    assert r["distance_m"] is not None and 0.5 < r["distance_m"] < 20
    assert r["depth_ms"] < 200
    est.close()
