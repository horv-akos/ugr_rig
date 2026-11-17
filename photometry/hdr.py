"""HDR merge - Debevec & Malik (1997) weighting on linear raw data.

Debevec's method has two halves:
  1. recover the response curve g(Z) = ln(E * dt) from the pixel values;
  2. merge:  ln E = sum_j w(Z_j) (g(Z_j) - ln dt_j) / sum_j w(Z_j)
     with the "hat" weight w(Z), low at both ends of the range.

Raw sensor data is already linear, so step 1 collapses to g = ln and only the
merge remains. Exposure normalisation (N^2 / (t * ISO)) takes the place of
ln dt, so the merged value is in the units the calibration constant k expects.

The hat is evaluated on the brightest Bayer channel of each quad, as a
fraction of the clip level, so a quad with one clipped channel gets zero
weight. On linear data the hat favours the longest unclipped exposure, which
is also the one with the best signal-to-noise ratio.

Frames are added one at a time, so only one decoded raw frame is in memory
on the Pi at any moment.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


def hat_weight(maxfrac: np.ndarray, floor: float) -> np.ndarray:
    w = np.where(maxfrac <= 0.5, maxfrac, 1.0 - maxfrac).astype(np.float32)
    w[(maxfrac >= 1.0) | (maxfrac < floor)] = 0.0
    return w


@dataclass
class HDRResult:
    rel: np.ndarray           # exposure-normalised signal, DN * N^2 / (t * ISO)
    weight: np.ndarray        # sum of hat weights; 0 where a fallback was used
    clipped: np.ndarray       # clipped even in the shortest frame -> value is a lower bound
    under: np.ndarray         # below the noise floor even in the longest frame
    pair_ratios: list[dict] = field(default_factory=list)
    n_frames: int = 0

    def stats(self) -> dict:
        n = self.rel.size
        return {
            "n_frames": self.n_frames,
            "clipped_px": int(self.clipped.sum()),
            "under_px": int(self.under.sum()),
            "clipped_fraction": float(self.clipped.sum() / n),
            "under_fraction": float(self.under.sum() / n),
            "pair_ratios": self.pair_ratios,
        }


class DebevecMerge:
    """Add frames in order of increasing exposure time, then call result()."""

    # Frame-to-frame consistency is measured where both frames sit comfortably
    # inside the linear range.
    RATIO_LO, RATIO_HI = 0.05, 0.8

    def __init__(self, noise_floor: float = 0.002) -> None:
        self.floor = noise_floor
        self._num = None
        self._den = None
        self._short = None      # (t, X, maxfrac) of the shortest exposure
        self._long = None       # (t, X) of the longest
        self._prev = None       # (label, X, maxfrac) for the consistency check
        self.pair_ratios: list[dict] = []
        self.n = 0

    def add(self, Y: np.ndarray, maxfrac: np.ndarray, t: float, f_number: float,
            iso: float, label: str = "") -> None:
        scale = np.float32(f_number * f_number / (t * iso))
        X = Y * scale
        w = hat_weight(maxfrac, self.floor)
        w[Y <= 0] = 0.0
        lnX = np.log(np.where(w > 0, X, 1.0), dtype=np.float32)
        if self._num is None:
            self._num = np.zeros_like(Y, dtype=np.float32)
            self._den = np.zeros_like(Y, dtype=np.float32)
        elif Y.shape != self._num.shape:
            raise ValueError("frames in a bracket must have the same size")
        self._num += w * lnX
        self._den += w

        if self._short is None or t < self._short[0]:
            self._short = (t, X, maxfrac)
        if self._long is None or t > self._long[0]:
            self._long = (t, X)

        if self._prev is not None:
            p_label, pX, pfrac = self._prev
            ok = ((pfrac > self.RATIO_LO) & (pfrac < self.RATIO_HI)
                  & (maxfrac > self.RATIO_LO) & (maxfrac < self.RATIO_HI) & (pX > 0))
            n_ok = int(ok.sum())
            ratio = float(np.median(X[ok] / pX[ok])) if n_ok >= 100 else None
            self.pair_ratios.append({"pair": [p_label, label], "ratio": ratio, "n_px": n_ok})
        self._prev = (label, X, maxfrac)
        self.n += 1

    def result(self) -> HDRResult:
        if self.n == 0:
            raise ValueError("no frames added")
        den = self._den
        have = den > 0
        rel = np.zeros_like(den)
        rel[have] = np.exp(self._num[have] / den[have])

        _, Xs, fs = self._short
        clipped = (~have) & (fs >= 1.0)
        rel[clipped] = Xs[clipped]                  # lower bound
        under = (~have) & (~clipped)
        rel[under] = np.maximum(self._long[1][under], 0.0)
        return HDRResult(rel=rel, weight=den, clipped=clipped, under=under,
                         pair_ratios=self.pair_ratios, n_frames=self.n)
