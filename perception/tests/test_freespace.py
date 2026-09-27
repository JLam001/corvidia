"""Sector free-space profile from a metric depth map (ported from the autodrone framework tests)."""

import math

import numpy as np

from corvidia_perception.freespace import band_rows, free_space_from_depth


def test_wall_on_left():
    d = np.full((48, 64), 5.0, dtype=np.float32)
    d[:, :20] = 0.5
    fs = free_space_from_depth(d, n_cols=9, d_stop=0.8, d_free=3.0)
    assert fs.columns[0] < 0.05 and fs.columns[-1] > 0.95
    assert not fs.all_close and fs.center_free > 0.9
    assert fs.nearest_m == 0.5


def test_all_close():
    d = np.full((48, 64), 0.4, dtype=np.float32)
    fs = free_space_from_depth(d, d_stop=0.8, d_free=3.0)
    assert fs.all_close and fs.center_free == 0.0


def test_center_blocked_side_open_is_not_all_close():
    d = np.full((48, 64), 5.0, dtype=np.float32)
    d[:, 20:44] = 0.5
    fs = free_space_from_depth(d, d_stop=0.8, d_free=3.0)
    assert not fs.all_close and fs.center_free < 0.4


def test_attitude_band_moves_with_pitch():
    level = band_rows(100, pitch_rad=0.0, vfov_deg=60, band_deg=15)
    nose_up = band_rows(100, pitch_rad=math.radians(10), vfov_deg=60, band_deg=15)
    assert level == (25, 75)
    assert nose_up[0] > level[0]  # horizon moves down the image when the nose comes up


def test_attitude_band_ignores_sky_wall():
    d = np.full((60, 64), 5.0, dtype=np.float32)
    d[:15, :] = 0.3  # something very close in the top rows: a ceiling, a branch above
    fixed = free_space_from_depth(d, band=(0.0, 1.0), d_stop=0.8, d_free=3.0)
    banded = free_space_from_depth(d, d_stop=0.8, d_free=3.0, pitch_rad=0.0, vfov_deg=60, band_deg=15)
    assert fixed.center_free < 0.1 and banded.center_free > 0.9


def test_wire_fields_match_autodrone_message():
    fs = free_space_from_depth(np.full((30, 70), 4.5, np.float32))
    assert set(fs.to_dict()) == {"t", "columns", "center_free", "all_close", "hfov_deg", "nearest_m"}
    assert len(fs.columns) == 7 and 0.0 <= fs.center_free <= 1.0
