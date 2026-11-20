"""k from the ISO rating (ISO 12232), without a meter."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from photometry.calibration import ISO12232_SATURATION, iso_estimate
from photometry.linearise import RawFrame

BLACK, WHITE = 2048.0, 15000.0
SPAN = WHITE - BLACK
W = (0.3, 0.5, 0.2)
ROI = (0.25, 0.25, 0.75, 0.75)


def frame(r: float, g: float, b: float, size: int = 40) -> RawFrame:
    """A flat RGGB frame with each channel at a fraction of white."""
    m = np.empty((size, size), np.uint16)
    m[0::2, 0::2] = BLACK + r * SPAN
    m[0::2, 1::2] = BLACK + g * SPAN
    m[1::2, 0::2] = BLACK + g * SPAN
    m[1::2, 1::2] = BLACK + b * SPAN
    return RawFrame(mosaic=m, cfa=np.array([[0, 1], [3, 2]], np.uint8), color_desc="RGBG",
                    black=np.full(4, BLACK), white=WHITE, cam_xyz=None, path=Path("f.npz"))


def test_neutral_raw_gives_78_over_q_span():
    e = iso_estimate(frame(0.5, 0.5, 0.5), W, ROI, q=0.65)
    assert e.dn_sat == pytest.approx(SPAN, rel=1e-3)
    assert e.k == pytest.approx(ISO12232_SATURATION / (0.65 * SPAN), rel=1e-3)
    assert e.cv < 1e-3 and e.exposure_fraction == pytest.approx(0.5, rel=1e-3)


def test_first_channel_to_clip_decides():
    # R is brightest: the mix is 0.55 of white when R is at 0.8, so it saturates at 0.55/0.8
    e = iso_estimate(frame(0.8, 0.5, 0.3), W, ROI)
    assert e.first_clip == "R"
    assert e.dn_sat == pytest.approx(SPAN * 0.55 / 0.8, rel=1e-3)
    # independent of exposure: the same colour one stop down gives the same k
    assert iso_estimate(frame(0.4, 0.25, 0.15), W, ROI).k == pytest.approx(e.k, rel=2e-3)


def test_iso_ratio_and_q_scale_k():
    base = iso_estimate(frame(0.5, 0.5, 0.5), W, ROI).k
    # a body whose true saturation ISO is 80 % of the label is less sensitive: larger k
    assert iso_estimate(frame(0.5, 0.5, 0.5), W, ROI, iso_ratio=0.8).k == pytest.approx(base / 0.8)
    assert iso_estimate(frame(0.5, 0.5, 0.5), W, ROI, q=0.5).k == pytest.approx(base * 0.65 / 0.5)


def test_clipped_or_dark_roi_is_refused():
    with pytest.raises(ValueError, match="well exposed"):
        iso_estimate(frame(0.99, 0.5, 0.5), W, ROI)
    with pytest.raises(ValueError, match="well exposed"):
        iso_estimate(frame(0.01, 0.01, 0.01), W, ROI)
    with pytest.raises(ValueError, match="q must"):
        iso_estimate(frame(0.5, 0.5, 0.5), W, ROI, q=1.5)
