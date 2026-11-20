"""Photometric calibration: exposure-normalised signal -> cd/m2.

    L = k * DN * N^2 / (t * ISO) / vignette * spectral_factor

`k` belongs to one camera + lens + aperture. It is stored in the camera profile and
re-measured each session on the matt target; the session value validates the
stored one rather than replacing it.

Without a meter, `iso_estimate` gives k from the camera's ISO rating (ISO 12232,
saturation-based speed). The standard defines the speed S from the focal-plane exposure
that just saturates the sensor, S = 78 / H_sat (lx s), and the exposure from the scene
luminance, H = q L t / N^2, with q = 0.65 for a typical lens (pi/4 times transmission and
vignetting). So the luminance that saturates is L_sat = 78 N^2 / (q S t), and with
L = k DN N^2 / (t S):

    k = 78 / (q * DN_sat)

DN_sat is the raw signal at saturation (white minus black level) seen through the pipeline's
luminance mix: the first channel to clip decides, so it depends on the light's colour and is
measured from a frame of a neutral surface. It needs no meter, but it is only as good as the
camera's ISO label (makers leave headroom; measured saturation ISOs of Canon bodies run below
the label) and q, so expect +-30 %. `iso_ratio` (measured saturation ISO / label, from
published sensor tests) corrects the first; the result is flagged either way.
"""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


def load_vignette(path: str, shape: tuple[int, int]) -> np.ndarray | None:
    """Vignetting map (1.0 on axis) at working resolution, or None."""
    if not path:
        return None
    v = np.load(Path(path)).astype(np.float32)
    if v.shape != shape:
        raise ValueError(f"vignetting map is {v.shape}, image is {shape} - "
                         "measured at a different binning?")
    if v.min() <= 0:
        raise ValueError("vignetting map has non-positive values")
    return v


def apply(rel: np.ndarray, k: float, vignette: np.ndarray | None = None) -> np.ndarray:
    """Relative signal -> luminance. With k <= 0 the result stays in relative units."""
    L = rel if vignette is None else rel / vignette
    return (L * np.float32(k)).astype(np.float32) if k > 0 else L.astype(np.float32)


def target_luminance(luminance: float | None = None, illuminance_lx: float | None = None,
                     reflectance: float | None = None) -> float:
    """Meter reading -> target luminance. Either a direct luminance reading, or
    illuminance on a Lambertian panel of known reflectance: L = rho * E / pi."""
    if luminance is not None:
        return float(luminance)
    if illuminance_lx is None or reflectance is None:
        raise ValueError("need a luminance, or an illuminance and the panel reflectance")
    return float(reflectance) * float(illuminance_lx) / math.pi


ISO12232_SATURATION = 78.0     # lx s: S_sat = 78 / H_sat, by definition (ISO 12232)
ISO_MIN_FRACTION = 0.05        # quads below this fraction of white are too noisy to use


@dataclass
class IsoEstimate:
    k: float
    roi: tuple[float, float, float, float]
    q: float
    iso_ratio: float
    dn_sat: float             # luminance-mix DN when the first channel reaches white
    white: float
    black: float              # mean black level
    exposure_fraction: float  # median brightest-channel level in the ROI, of white
    first_clip: str           # channel that clips first (R, G or B)
    cv: float                 # spread of dn_sat over the ROI: colour uniformity of the surface
    n_px: int
    frame: str
    shutter: str
    k_stored: float
    drift: float | None
    drift_flag: bool
    method: str = "iso"

    def as_dict(self) -> dict:
        return asdict(self)


def quad_rgb(frame, roi: tuple[float, float, float, float]) -> tuple[np.ndarray, np.ndarray]:
    """Black-subtracted R, G, B per 2x2 quad inside `roi`, (3, h, w) float32, and each
    channel's span from black to white."""
    h, w = frame.mosaic.shape
    sy, sx = roi_slice(roi, (h // 2, w // 2))
    m = frame.mosaic[2 * sy.start:2 * sy.stop, 2 * sx.start:2 * sx.stop]
    acc: dict[str, list] = {"R": [], "G": [], "B": []}
    span: dict[str, list] = {"R": [], "G": [], "B": []}
    for dy in (0, 1):
        for dx in (0, 1):
            ch = int(frame.cfa[dy, dx])
            letter = frame.color_desc[ch]
            if letter not in acc:
                raise ValueError(f"unsupported CFA colour {letter!r}")
            black = float(frame.black[ch])
            acc[letter].append(m[dy::2, dx::2].astype(np.float32) - np.float32(black))
            span[letter].append(frame.white - black)
    rgb = np.stack([sum(acc[c]) / np.float32(len(acc[c])) for c in "RGB"])
    return rgb, np.array([float(np.mean(span[c])) for c in "RGB"])


def iso_estimate(frame, weights: tuple[float, float, float],
                 roi: tuple[float, float, float, float], q: float = 0.65,
                 iso_ratio: float = 1.0, saturation_fraction: float = 0.92,
                 k_stored: float = 0.0, drift_warn: float = 0.10,
                 shutter: str = "") -> IsoEstimate:
    """k from the ISO rating, using one raw frame of a neutral surface inside `roi`."""
    if not (0.2 <= q <= 1.0):
        raise ValueError("q must be 0.2 to 1 (ISO 12232 uses 0.65)")
    if not (0.25 <= iso_ratio <= 4.0):
        raise ValueError("the saturation ISO ratio must be 0.25 to 4")
    rgb, spans = quad_rgb(frame, roi)
    frac = (rgb / spans[:, None, None].astype(np.float32)).max(axis=0)
    ok = (frac >= ISO_MIN_FRACTION) & (frac < saturation_fraction)
    if int(ok.sum()) < max(16, ok.size // 2):
        raise ValueError("the ROI is not well exposed in this frame (too dark or clipped)")
    Y = np.tensordot(np.asarray(weights, np.float32), rgb, axes=1)
    ysat = Y[ok] / frac[ok]                    # mix DN when the brightest channel hits white
    dn_sat = float(np.median(ysat))
    if dn_sat <= 0:
        raise ValueError("no signal in the ROI")
    first = "RGB"[int(np.bincount((rgb / spans[:, None, None]).argmax(axis=0)[ok],
                                  minlength=3).argmax())]
    k = ISO12232_SATURATION / (q * iso_ratio * dn_sat)
    drift = (k - k_stored) / k_stored if k_stored > 0 else None
    return IsoEstimate(
        k=k, roi=tuple(roi), q=q, iso_ratio=iso_ratio, dn_sat=dn_sat, white=float(frame.white),
        black=float(np.mean(frame.black)), exposure_fraction=float(np.median(frac[ok])),
        first_clip=first, cv=float(np.std(ysat) / np.mean(ysat)), n_px=int(ok.sum()),
        frame=frame.path.name, shutter=shutter, k_stored=k_stored, drift=drift,
        drift_flag=drift is not None and abs(drift) > drift_warn)


@dataclass
class KMeasurement:
    k: float
    target_cd_m2: float
    roi: tuple[float, float, float, float]
    mean_rel: float
    cv: float                 # coefficient of variation inside the ROI - panel uniformity
    clipped_px: int
    n_px: int
    k_stored: float
    drift: float | None       # (k - k_stored) / k_stored
    drift_flag: bool

    def as_dict(self) -> dict:
        return asdict(self)


def roi_slice(roi: tuple[float, float, float, float], shape: tuple[int, int]):
    """Normalised (x0, y0, x1, y1) -> numpy slices."""
    h, w = shape
    x0, y0, x1, y1 = roi
    if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
        raise ValueError(f"ROI {roi} must be normalised x0 < x1, y0 < y1 within 0..1")
    return slice(int(y0 * h), max(int(y1 * h), int(y0 * h) + 1)), \
        slice(int(x0 * w), max(int(x1 * w), int(x0 * w) + 1))


def measure_k(rel: np.ndarray, clipped: np.ndarray, roi: tuple[float, float, float, float],
              target_cd_m2: float, vignette: np.ndarray | None = None,
              k_stored: float = 0.0, drift_warn: float = 0.10) -> KMeasurement:
    sy, sx = roi_slice(roi, rel.shape)
    patch = rel[sy, sx] if vignette is None else rel[sy, sx] / vignette[sy, sx]
    n_clip = int(clipped[sy, sx].sum())
    if n_clip:
        raise ValueError(f"{n_clip} clipped pixels inside the calibration ROI")
    mean = float(patch.mean())
    if mean <= 0:
        raise ValueError("calibration ROI has no signal")
    cv = float(patch.std() / mean)
    if cv > 0.05:
        log.warning("calibration ROI is not uniform (cv %.1f %%) - ROI off the panel?", 100 * cv)
    k = target_cd_m2 / mean
    drift = (k - k_stored) / k_stored if k_stored > 0 else None
    flag = drift is not None and abs(drift) > drift_warn
    if flag:
        log.warning("k drifted %.1f %% from the stored value - lens, aperture, filter, "
                    "or in-camera processing changed?", 100 * drift)
    return KMeasurement(k=k, target_cd_m2=target_cd_m2, roi=tuple(roi), mean_rel=mean, cv=cv,
                        clipped_px=n_clip, n_px=int(patch.size), k_stored=k_stored,
                        drift=drift, drift_flag=flag)
