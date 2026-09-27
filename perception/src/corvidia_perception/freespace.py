"""Depth map -> per-sector obstacle free-space profile (pure NumPy).

Ported from the autodrone framework's l2_perception/freespace.py (same field names, so the
profile can be forwarded to its `perc.freespace` topic unchanged); metric depth only, which is
what the Depth Anything V2 Metric engine produces.

    nearest = sector_nearest(depth_m, n_cols, band, near_percentile)   metres, left to right
    fs      = free_space_from_nearest(nearest, d_stop, d_free, hfov_deg, t)
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import numpy as np


@dataclass
class FreeSpace:
    """Per-column free score 0..1, left to right, spanning hfov_deg. all_close = nothing safe ahead."""
    t: float = 0.0                       # publish time (wall clock)
    columns: list = field(default_factory=list)
    center_free: float = 0.0
    all_close: bool = False
    hfov_deg: float = 90.0
    nearest_m: float = -1.0

    def to_dict(self) -> dict:
        return asdict(self)


def band_rows(h: int, band=(0.35, 0.8), pitch_rad: float | None = None, vfov_deg: float | None = None,
              band_deg: float | None = None) -> tuple[int, int]:
    """Rows to keep. With attitude: +-band_deg around the horizon, whose row moves with pitch
    (nose up -> horizon lower in the image). Without: the fixed fraction `band`."""
    if pitch_rad is not None and vfov_deg and band_deg:
        vfov = math.radians(vfov_deg)
        horizon = 0.5 + pitch_rad / vfov
        half = math.radians(band_deg) / vfov
        r0, r1 = int((horizon - half) * h), int((horizon + half) * h)
    else:
        r0, r1 = int(band[0] * h), int(band[1] * h)
    r0 = max(0, min(h - 1, r0))
    r1 = max(r0 + 1, min(h, r1))
    return r0, r1


def sector_nearest(depth_m: np.ndarray, n_cols: int = 7, band=(0.35, 0.8), near_percentile: float = 5.0,
                   pitch_rad: float | None = None, vfov_deg: float | None = None,
                   band_deg: float | None = None) -> np.ndarray:
    """Low percentile of metric depth per sector = the nearest thing in that direction."""
    r0, r1 = band_rows(depth_m.shape[0], band, pitch_rad, vfov_deg, band_deg)
    rows = depth_m[r0:r1]
    cols = np.array_split(rows, n_cols, axis=1)
    return np.array([np.percentile(c, near_percentile) for c in cols], dtype=float)


def free_space_from_nearest(nearest: np.ndarray, d_stop: float = 3.0, d_free: float = 6.0,
                            hfov_deg: float = 65.0, t: float = 0.0) -> FreeSpace:
    n = len(nearest)
    mid = n // 2
    free = np.clip((nearest - d_stop) / max(1e-6, d_free - d_stop), 0.0, 1.0)
    center_blocked = bool(nearest[mid] <= d_stop)
    center_free = float(np.mean(free[max(0, mid - 1): mid + 2]))
    all_close = bool(center_blocked and free.max() < 0.2)
    return FreeSpace(t=t, columns=[float(x) for x in free], center_free=center_free, all_close=all_close,
                     hfov_deg=hfov_deg, nearest_m=float(nearest.min()))


def free_space_from_depth(depth_m: np.ndarray, n_cols: int = 7, band=(0.35, 0.8), near_percentile: float = 5.0,
                          d_stop: float = 3.0, d_free: float = 6.0, hfov_deg: float = 65.0, t: float = 0.0,
                          pitch_rad: float | None = None, vfov_deg: float | None = None,
                          band_deg: float | None = None) -> FreeSpace:
    nearest = sector_nearest(depth_m, n_cols, band, near_percentile, pitch_rad, vfov_deg, band_deg)
    return free_space_from_nearest(nearest, d_stop, d_free, hfov_deg, t)
