"""Best-shot selection: keep the clearest crop per track, save it when the track ends."""

import json

import numpy as np

from corvidia_perception.best_shot import shot_score
from corvidia_perception.records import BBox, Detection

from conftest import person


def test_sharper_larger_unclipped_view_scores_higher():
    rng = np.random.default_rng(0)
    sharp = rng.integers(0, 255, (200, 100, 3), dtype=np.uint8)
    blurry = np.full((200, 100, 3), 120, np.uint8)
    det = Detection(1, BBox(100, 100, 200, 300), 0.8)
    s_sharp, _ = shot_score(sharp, det, 640, 480)
    s_blur, _ = shot_score(blurry, det, 640, 480)
    assert s_sharp > s_blur
    small = Detection(1, BBox(100, 100, 150, 200), 0.8)
    assert shot_score(sharp, det, 640, 480)[0] > shot_score(sharp[:100, :50], small, 640, 480)[0]
    edge = Detection(1, BBox(0, 100, 100, 300), 0.8)
    s_edge, meta = shot_score(sharp, edge, 640, 480)
    assert meta["at_frame_edge"] and s_edge < s_sharp


def test_best_shot_saved_when_track_ends(harness):
    from corvidia_perception.records import Frame, FrameInfo

    from conftest import H, W

    h = harness()
    rng = np.random.default_rng(1)
    h.frames(6, person(1))  # flat frames: sharpness 0
    for _ in range(3):      # three sharp frames; at least one lands on a scored frame
        h.frame_id += 1
        image = rng.integers(0, 255, (H, W, 3), dtype=np.uint8)
        h.pipe.on_frame(Frame(FrameInfo("test", h.epoch, h.frame_id, W, H, h.clock(), 0.0), image),
                        [person(1)])
        h.pipe.tick()
        h.clock.advance(h.dt)
    h.frames(3, person(1))
    h.idle(2.0)  # track lost -> best shot written
    (event,) = h.events()
    best = h.session_dir / event["event_id"] / "best.jpg"
    assert best.exists() and best.stat().st_size > 0
    meta = json.loads((best.parent / "best.json").read_text())
    assert meta["track_id"] == 1 and meta["sharpness"] > 0
    assert h.pipe.health.count("best_shots_saved") == 1


def test_best_shots_flushed_on_stop(harness):
    h = harness()
    h.frames(5, person(1))
    h.pipe.stop()
    (event,) = h.events()
    assert (h.session_dir / event["event_id"] / "best.jpg").exists()
