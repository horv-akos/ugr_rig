"""Small bright spots and image shifts in live-view frames: the camera as the head's position
sensor.

The head is open loop: it counts steps and never sees where it points. The camera closes the
loop. The fiducial (a small bright dot, fixed in the cab for the session; its design and
mounting are still open, REQUIREMENTS §9) is found near where the zero expects it, and its
centroid in pixels becomes an angle through the lens geometry. The same centroid tracks the dot
across the frame for the lens fit (photometry.lensfit). Nothing here assumes more about the dot
than that it is small and brighter than what surrounds it.

The aim check compares a whole frame with the position's reference image by phase correlation:
the shift of the peak of the normalised cross-power spectrum is the translation between the
two, to a fraction of a pixel, and it does not care about the overall brightness.

Live-view frames, not raw: a frame is there in a fraction of a second at any pose, and at
~1000 px across 64 deg one pixel is ~0.06 deg, so a centroid to a tenth of a pixel is well
inside the 0.05 deg zero budget. That rests on live view showing the same field as the raw
frame (open item, REQUIREMENTS §9).
"""

from __future__ import annotations

import io
from dataclasses import asdict, dataclass

import numpy as np


class SpotNotFound(ValueError):
    """No small bright spot where one was expected."""


def gray(image: bytes | np.ndarray) -> np.ndarray:
    """A JPEG (bytes) or an RGB/grey array -> float32 grey levels 0..255 (Rec. 601 luma)."""
    if isinstance(image, (bytes, bytearray)):
        from PIL import Image

        with Image.open(io.BytesIO(image)) as im:
            arr = np.asarray(im.convert("RGB"))
    else:
        arr = np.asarray(image)
    if arr.ndim == 3:
        arr = arr[..., 0] * 0.299 + arr[..., 1] * 0.587 + arr[..., 2] * 0.114
    return arr.astype(np.float32)


@dataclass(frozen=True)
class Spot:
    u: float                 # column, pixels, sub-pixel centroid
    v: float                 # row
    peak: float              # grey level
    background: float        # median grey level of the search window
    n_px: int
    extent_px: float         # largest side of the spot's bounding box
    saturated: bool          # the peak is at 255: the centroid is still good, the peak is not

    def as_dict(self) -> dict:
        return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in asdict(self).items()}


def find_spot(img: np.ndarray, centre: tuple[float, float], radius_px: float,
              max_extent_px: float, min_contrast: float) -> Spot:
    """The brightest compact blob within `radius_px` of `centre` (u, v).

    The blob is everything connected to the brightest pixel above half-way between the
    window's median and that pixel. Its centroid is weighted by the signal above the median.
    Refused (SpotNotFound) when nothing stands `min_contrast` above the surroundings, when the
    blob is larger than `max_extent_px`, or when it runs into the edge of the window.
    """
    from scipy import ndimage

    h, w = img.shape
    cu, cv = centre
    u0, u1 = int(max(0, np.floor(cu - radius_px))), int(min(w, np.ceil(cu + radius_px) + 1))
    v0, v1 = int(max(0, np.floor(cv - radius_px))), int(min(h, np.ceil(cv + radius_px) + 1))
    if u1 - u0 < 3 or v1 - v0 < 3:
        raise SpotNotFound(f"the search window at ({cu:.0f}, {cv:.0f}) is outside the frame")
    win = img[v0:v1, u0:u1]
    vv, uu = np.mgrid[v0:v1, u0:u1]
    inside = (uu - cu) ** 2 + (vv - cv) ** 2 <= radius_px ** 2
    bg = float(np.median(win[inside]))
    masked = np.where(inside, win, -np.inf)
    iv, iu = np.unravel_index(int(np.argmax(masked)), win.shape)
    peak = float(win[iv, iu])
    if peak - bg < min_contrast:
        raise SpotNotFound(f"nothing bright within {radius_px:.0f} px of ({cu:.0f}, {cv:.0f}): "
                           f"brightest {peak:.0f} on a background of {bg:.0f}")
    labels, _ = ndimage.label((win >= bg + 0.5 * (peak - bg)) & inside)
    blob = labels == labels[iv, iu]
    rows, cols = np.nonzero(blob)
    extent = float(max(rows.max() - rows.min(), cols.max() - cols.min()) + 1)
    if extent > max_extent_px:
        raise SpotNotFound(f"the brightest thing near ({cu:.0f}, {cv:.0f}) is {extent:.0f} px "
                           f"across, more than {max_extent_px:.0f}: not a small spot")
    edge = (np.hypot(uu[blob] - cu, vv[blob] - cv) > radius_px - 1.5).any()
    if edge:
        raise SpotNotFound(f"the brightest blob near ({cu:.0f}, {cv:.0f}) runs into the edge "
                           "of the search window")
    # The centroid takes the blob's edges too (grown by 2 px): cut at half height, a spot
    # between pixel centres loses more of one edge than the other.
    near = ndimage.binary_dilation(blob, iterations=2) & inside
    wgt = np.clip(win[near] - bg, 0, None)
    return Spot(u=float((uu[near] * wgt).sum() / wgt.sum()),
                v=float((vv[near] * wgt).sum() / wgt.sum()),
                peak=peak, background=bg, n_px=int(blob.sum()), extent_px=extent,
                saturated=peak >= 254.5)


@dataclass(frozen=True)
class Shift:
    du: float                # content moved right by du pixels in `frame` against `ref`
    dv: float                # content moved down by dv pixels
    peak: float              # height of the correlation peak, 0..1: ~1 same scene, < 0.1 doubtful

    def as_dict(self) -> dict:
        return {k: round(v, 4) for k, v in asdict(self).items()}


def phase_correlate(ref: np.ndarray, frame: np.ndarray) -> Shift:
    """Translation of `frame` against `ref` (same shape, grey), by phase correlation with a
    Hann window, refined to sub-pixel by a parabola through the peak."""
    if ref.shape != frame.shape:
        raise ValueError(f"reference is {ref.shape}, frame is {frame.shape}")
    h, w = ref.shape
    win = np.outer(np.hanning(h), np.hanning(w)).astype(np.float32)
    fa = np.fft.rfft2((ref - ref.mean()) * win)
    fb = np.fft.rfft2((frame - frame.mean()) * win)
    cross = fb * np.conj(fa)
    cross /= np.maximum(np.abs(cross), 1e-12)
    corr = np.fft.irfft2(cross, s=(h, w))
    pv, pu = np.unravel_index(int(np.argmax(corr)), corr.shape)

    def sub(c_m: float, c_0: float, c_p: float) -> float:
        # Foroosh, Zerubia & Berthod (2002): the phase-correlation peak of a sub-pixel shift
        # is a sampled Dirichlet kernel, so the larger neighbour n gives d = n / (n +- c0);
        # the root inside (0, 1) is the shift. A parabola is biased by ~0.1 px here.
        n, sign = (c_p, 1.0) if c_p >= c_m else (c_m, -1.0)
        for d in (n / (n + c_0) if n + c_0 else 2.0, n / (n - c_0) if n - c_0 else 2.0):
            if 0.0 <= d < 1.0:
                return float(sign * d)
        return 0.0

    du = pu + sub(corr[pv, (pu - 1) % w], corr[pv, pu], corr[pv, (pu + 1) % w])
    dv = pv + sub(corr[(pv - 1) % h, pu], corr[pv, pu], corr[(pv + 1) % h, pu])
    du = du - w if du > w / 2 else du
    dv = dv - h if dv > h / 2 else dv
    return Shift(du=float(du), dv=float(dv), peak=float(corr[pv, pu]))
