"""Frame conversion out of GStreamer buffers, without a camera."""
import numpy as np
import pytest

from corvidia_perception.sources import argus_pipeline, bgr_from_buffer


def test_argus_pipeline_has_no_cpu_videoconvert():
    pipeline = argus_pipeline(0, 1, (1280, 720), 30)
    assert "videoconvert" not in pipeline
    assert "format=BGRx ! appsink name=sink" in pipeline


def test_bgrx_buffer_drops_pad_byte_into_owned_contiguous_bgr():
    h, w = 3, 5
    bgrx = np.arange(h * w * 4, dtype=np.uint8).reshape(h, w, 4)
    data = bytearray(bgrx.tobytes())
    image = bgr_from_buffer(data, w, h, "BGRx")
    assert image.shape == (h, w, 3) and image.flags.c_contiguous
    assert np.array_equal(image, bgrx[:, :, :3])
    data[0] = 255  # the GStreamer buffer is unmapped after read; the frame must not alias it
    assert image[0, 0, 0] == 0


def test_bgr_buffer_is_copied_unchanged():
    bgr = np.arange(2 * 4 * 3, dtype=np.uint8).reshape(2, 4, 3)
    data = bytearray(bgr.tobytes())
    image = bgr_from_buffer(data, 4, 2, "BGR")
    assert np.array_equal(image, bgr)
    data[0] = 255
    assert image[0, 0, 0] == 0


def test_unknown_capture_format_is_rejected():
    with pytest.raises(ValueError, match="NV12"):
        bgr_from_buffer(b"\0" * 24, 4, 2, "NV12")
