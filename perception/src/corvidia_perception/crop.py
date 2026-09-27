"""Crop selection, quality checks, and frozen copies."""

from __future__ import annotations

import math

import numpy as np

from .config import CropConfig
from .records import BBox, CropRegion


def padded_region(bbox: BBox, width: int, height: int, pad_fraction: float) -> CropRegion | None:
    """Pad each side by a fraction of the box size and clamp to the image.

    Returns None when the clamped region is empty.
    """
    pad_x = bbox.width * pad_fraction
    pad_y = bbox.height * pad_fraction
    x1 = max(0, math.floor(bbox.x1 - pad_x))
    y1 = max(0, math.floor(bbox.y1 - pad_y))
    x2 = min(width, math.ceil(bbox.x2 + pad_x))
    y2 = min(height, math.ceil(bbox.y2 + pad_y))
    if x2 <= x1 or y2 <= y1:
        return None
    return CropRegion(x1, y1, x2, y2)


def sharpness(crop: np.ndarray) -> float:
    """Variance of a 4-neighbour Laplacian on the grey image."""
    grey = crop.astype(np.float32)
    if grey.ndim == 3:
        grey = grey.mean(axis=2)
    if grey.shape[0] < 3 or grey.shape[1] < 3:
        return 0.0
    lap = (
        grey[:-2, 1:-1] + grey[2:, 1:-1] + grey[1:-1, :-2] + grey[1:-1, 2:]
        - 4.0 * grey[1:-1, 1:-1]
    )
    return float(lap.var())


def bbox_passes_size(bbox: BBox, cfg: CropConfig) -> bool:
    return bbox.width >= cfg.min_width_px and bbox.height >= cfg.min_height_px


def select_crop(image: np.ndarray, bbox: BBox, cfg: CropConfig) -> tuple[CropRegion, np.ndarray] | None:
    """Return the padded region and a private read-only copy, or None if it fails quality."""
    if not bbox_passes_size(bbox, cfg):
        return None
    h, w = image.shape[:2]
    region = padded_region(bbox, w, h, cfg.pad_fraction)
    if region is None:
        return None
    crop = freeze(image[region.y1:region.y2, region.x1:region.x2])
    if cfg.min_sharpness > 0 and sharpness(crop) < cfg.min_sharpness:
        return None
    return region, crop


def freeze(array: np.ndarray) -> np.ndarray:
    """Copy into memory the source cannot overwrite, and make it read-only."""
    out = np.array(array, copy=True, order="C")
    out.flags.writeable = False
    return out
