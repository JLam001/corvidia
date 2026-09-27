"""Unit checks for parsing, crops, config, and the frame slot."""

import threading

import numpy as np
import pytest

from corvidia_perception.config import load_config
from corvidia_perception.confirmer import parse_answer
from corvidia_perception.crop import padded_region, select_crop, sharpness
from corvidia_perception.config import CropConfig
from corvidia_perception.pipeline import LatestFrameSlot
from corvidia_perception.records import BBox, Frame, FrameInfo, Result
from corvidia_perception import simulate


@pytest.mark.parametrize("text, expected", [
    ('{"answer": "yes"}', (Result.CONFIRMED, "answer_yes")),
    ('{"answer": "no"}', (Result.REJECTED, "answer_no")),
    ('{"answer": "uncertain"}', (Result.UNKNOWN, "ambiguous")),
    ('yes', (Result.UNKNOWN, "malformed_output")),
    ('Yes, there is a person.', (Result.UNKNOWN, "malformed_output")),
    ('{"answer": "yes, probably"}', (Result.UNKNOWN, "malformed_output")),
    ('{"reasoning": "yes", "answer": "no"}', (Result.REJECTED, "answer_no")),
    ('["yes"]', (Result.UNKNOWN, "malformed_output")),
    ('{"answer": "yes"', (Result.UNKNOWN, "malformed_output")),
])
def test_parse_answer_reads_only_the_answer_field(text, expected):
    assert parse_answer(text) == expected


def test_padding_is_20_percent_per_side_and_clamped():
    r = padded_region(BBox(100, 100, 150, 200), 640, 480, 0.2)
    assert r.as_list() == [90, 80, 160, 220]
    r = padded_region(BBox(0, 400, 50, 480), 640, 480, 0.2)
    assert r.as_list() == [0, 384, 60, 480]


def test_quality_gate_rejects_tiny_and_blurry_crops():
    image = np.zeros((240, 320, 3), dtype=np.uint8)
    assert select_crop(image, BBox(10, 10, 20, 20), CropConfig()) is None
    cfg = CropConfig(min_sharpness=5.0)
    assert select_crop(image, BBox(10, 10, 60, 110), cfg) is None  # flat image
    rng = np.random.default_rng(0)
    noisy = rng.integers(0, 255, image.shape, dtype=np.uint8)
    assert sharpness(noisy) > 5.0
    region, crop = select_crop(noisy, BBox(10, 10, 60, 110), cfg)
    assert crop.shape[:2] == (region.height, region.width)


def test_load_config(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('[gate]\nmin_hits = 4\n[storage]\nroot = "/data/events"\n')
    cfg = load_config(path)
    assert cfg.gate.min_hits == 4 and cfg.gate.window_frames == 5
    assert str(cfg.storage.root) == "/data/events"
    path.write_text('[gate]\nmin_hit = 4\n')
    with pytest.raises(ValueError):
        load_config(path)


def test_latest_frame_slot_keeps_only_newest():
    slot = LatestFrameSlot()
    frames = [Frame(FrameInfo("t", 0, i, 1, 1, 0.0, 0.0), np.zeros((1, 1))) for i in range(3)]
    for f in frames:
        slot.put(f)
    assert slot.take(0).info.frame_id == 2
    assert slot.take(0) is None

    got = []
    t = threading.Thread(target=lambda: got.append(slot.take(2.0)))
    t.start()
    slot.put(frames[0])
    t.join()
    assert got[0].info.frame_id == 0


def test_threaded_simulation_smoke(tmp_path, capsys):
    simulate.main(["--events", str(tmp_path), "--seconds", "3", "--inference-delay", "0.2"])
    out = capsys.readouterr().out
    assert "track 1: confirmed" in out
    assert "track 3: confirmed" in out
    assert "track 2" not in out
