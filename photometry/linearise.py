"""Raw frame -> linear, black-subtracted luminance signal at working resolution.

No demosaic. Each 2x2 Bayer quad (R, G, G, B) becomes one pixel, which is the
"downscale early" step for free: 6000 x 4000 -> 3000 x 2000, every value a real
measurement, and no interpolation to bleed a bright LED into its neighbours.

The channels are mixed with the Y row of the camera's colour matrix - the mix
closest to the eye's V(lambda) response this sensor can give. The weights are
normalised to sum to 1; the absolute scale belongs to the calibration constant k.

Clipping is judged per raw channel, before the mix: a quad with any channel at
the clip level is clipped, however the others look.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

FALLBACK_WEIGHTS = (0.0, 1.0, 0.0)   # green only: the nearest single channel to V(lambda)


@dataclass
class RawFrame:
    mosaic: np.ndarray            # (H, W) uint16, visible area, even dimensions
    cfa: np.ndarray               # (2, 2) channel index into color_desc
    color_desc: str               # e.g. "RGBG"
    black: np.ndarray             # (4,) per channel index
    white: float
    cam_xyz: np.ndarray | None    # (3, 3) XYZ -> camera, from the raw file
    path: Path


def read_frame(path: str | Path, fmt: str) -> RawFrame:
    path = Path(path)
    if fmt == "cr2":
        import rawpy

        with rawpy.imread(str(path)) as raw:
            mosaic = np.array(raw.raw_image_visible, dtype=np.uint16, copy=True)
            colors = np.array(raw.raw_colors_visible[:2, :2], dtype=np.uint8)
            desc = raw.color_desc.decode() if isinstance(raw.color_desc, bytes) else str(raw.color_desc)
            black = np.array(raw.black_level_per_channel, dtype=np.float64)
            white = float(raw.white_level)
            m = np.array(raw.rgb_xyz_matrix, dtype=np.float64)[:3]
            cam_xyz = m if np.any(m) else None
    elif fmt == "npz":
        with np.load(path) as z:
            mosaic = z["mosaic"].astype(np.uint16)
            colors = z["cfa"].astype(np.uint8)
            desc = str(z["color_desc"])
            black = z["black"].astype(np.float64)
            white = float(z["white"])
            cam_xyz = z["cam_xyz"] if "cam_xyz" in z.files else None
    else:
        raise ValueError(f"unknown frame format {fmt!r}")

    h, w = mosaic.shape
    mosaic = mosaic[: h - h % 2, : w - w % 2]
    return RawFrame(mosaic=mosaic, cfa=colors, color_desc=desc, black=black,
                    white=white, cam_xyz=cam_xyz, path=path)


def luminance_weights(frame: RawFrame, setting: str | tuple[float, float, float]
                      ) -> tuple[tuple[float, float, float], str]:
    """(wR, wG, wB) summing to 1, and a note on where they came from."""
    if not isinstance(setting, str):
        w = np.array(setting, dtype=np.float64)
        return tuple(w / w.sum()), "config"
    if frame.cam_xyz is None:
        log.warning("%s: no colour matrix in file; using green only", frame.path.name)
        return FALLBACK_WEIGHTS, "fallback-green"
    # camera = M @ XYZ  ->  XYZ = inv(M) @ camera; Y is the middle row
    y_row = np.linalg.inv(frame.cam_xyz)[1]
    if y_row.sum() <= 0:
        log.warning("%s: colour matrix gives a degenerate Y row; using green only", frame.path.name)
        return FALLBACK_WEIGHTS, "fallback-green"
    return tuple(float(x) for x in y_row / y_row.sum()), "matrix"


def superpixel(frame: RawFrame, weights: tuple[float, float, float],
               saturation_fraction: float, binning: int = 2
               ) -> tuple[np.ndarray, np.ndarray]:
    """Return (Y, maxfrac) at working resolution.

    Y        black-subtracted, channel-mixed signal in DN (float32)
    maxfrac  brightest channel of the quad as a fraction of the clip level;
             >= 1 means clipped (float32)
    """
    letters = {"R": 0, "G": 1, "B": 2}
    acc = {0: [], 1: [], 2: []}
    maxfrac = None
    for dy in (0, 1):
        for dx in (0, 1):
            ch = int(frame.cfa[dy, dx])
            letter = frame.color_desc[ch]
            if letter not in letters:
                raise ValueError(f"unsupported CFA colour {letter!r} ({frame.color_desc})")
            black = frame.black[ch]
            clip = saturation_fraction * (frame.white - black)
            plane = frame.mosaic[dy::2, dx::2].astype(np.float32) - np.float32(black)
            acc[letters[letter]].append(plane)
            frac = plane / np.float32(clip)
            maxfrac = frac if maxfrac is None else np.maximum(maxfrac, frac)

    if any(not v for v in acc.values()):
        raise ValueError(f"CFA {frame.color_desc} does not contain R, G and B")
    rgb = [sum(v) / np.float32(len(v)) for v in (acc[0], acc[1], acc[2])]
    Y = (np.float32(weights[0]) * rgb[0] + np.float32(weights[1]) * rgb[1]
         + np.float32(weights[2]) * rgb[2])

    extra = binning // 2
    if extra > 1:
        Y = block_mean(Y, extra)
        maxfrac = block_max(maxfrac, extra)
    return Y.astype(np.float32), maxfrac.astype(np.float32)


def block_mean(a: np.ndarray, n: int) -> np.ndarray:
    h, w = a.shape[0] // n * n, a.shape[1] // n * n
    return a[:h, :w].reshape(h // n, n, w // n, n).mean(axis=(1, 3))


def block_max(a: np.ndarray, n: int) -> np.ndarray:
    h, w = a.shape[0] // n * n, a.shape[1] // n * n
    return a[:h, :w].reshape(h // n, n, w // n, n).max(axis=(1, 3))


def frame_info(frame: RawFrame) -> dict[str, Any]:
    return {"file": frame.path.name, "shape": list(frame.mosaic.shape),
            "black": frame.black.tolist(), "white": frame.white,
            "color_desc": frame.color_desc}
