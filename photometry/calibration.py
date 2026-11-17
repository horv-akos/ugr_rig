"""Photometric calibration: exposure-normalised signal -> cd/m2.

    L = k * DN * N^2 / (t * ISO) / vignette * spectral_factor

`k` belongs to one camera + lens + aperture. It is stored in analysis.toml and
re-measured each session on the matt target; the session value validates the
stored one rather than replacing it.
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
