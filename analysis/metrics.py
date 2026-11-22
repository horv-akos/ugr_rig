"""Numbers beyond the UGR itself (REQUIREMENTS §5.6): what a design engineer can act on.

Per source:
    share       its part of the position's sum of L^2 omega / p^2
    delta_ugr   UGR with it minus UGR without it: what removing (or fixing) that one light buys
    e_eye_lx    direct illuminance at the eye, on a plane facing the line of sight: L omega cos sigma
    contrast    L / L_b

Per position:
    luminance percentiles (L50, L90, L99, per pixel, excluded areas left out)
    fraction of the frame's solid angle at or above the threshold
    threshold sensitivity: the UGR again at other detection thresholds, which shows how much
    the number depends on that choice

UGR values are never averaged: the scale is logarithmic.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from photometry.masks import CameraGeometry

from . import detection, ugr as ugr_mod


def _ugr_of(total: float, L_b: float) -> float | None:
    if L_b <= 0 or total <= 0:
        return None
    return 8.0 * math.log10(0.25 / L_b * total)


def rate_extras(sources: list, L_b: float) -> None:
    """Fill share, delta_ugr, e_eye_lx and contrast on rated sources, in place."""
    total = sum(s.contribution for s in sources)
    full = _ugr_of(total, L_b)
    for s in sources:
        s.share = s.contribution / total if total > 0 else 0.0
        without = _ugr_of(total - s.contribution, L_b)
        s.delta_ugr = None if full is None or without is None else full - without
        sig = math.radians(s.sigma_deg) if s.sigma_deg == s.sigma_deg else math.pi / 2
        s.e_eye_lx = s.L_cd_m2 * s.omega_sr * max(0.0, math.cos(sig))
        s.contrast = s.L_cd_m2 / L_b if L_b > 0 else float("nan")


def position_metrics(L: np.ndarray, omega: np.ndarray, exclude: np.ndarray, threshold: float,
                     sources: list) -> dict[str, Any]:
    keep = ~exclude
    vals = L[keep]
    w = omega[keep].astype(np.float64)
    pct = np.percentile(vals, [50, 90, 99]) if vals.size else [float("nan")] * 3
    above = (L >= threshold) & keep
    return {
        "L50_cd_m2": float(pct[0]), "L90_cd_m2": float(pct[1]), "L99_cd_m2": float(pct[2]),
        "L_max_cd_m2": float(vals.max()) if vals.size else float("nan"),
        "fraction_above_threshold": float(omega[above].sum() / w.sum()) if w.sum() else 0.0,
        "e_eye_direct_lx": float(sum(s.e_eye_lx for s in sources)),
        "sum_contribution": float(sum(s.contribution for s in sources)),
    }


def threshold_sensitivity(L: np.ndarray, clipped: np.ndarray, geom: CameraGeometry,
                          masks: detection.RegionMasks, thresholds: tuple[float, ...],
                          min_pixels: int, merge_radius_px: int, spectral, cam_to_view,
                          ugr_cfg, L_b: float, lighting: str = "dark",
                          reflection_threshold: float | None = None,
                          reflection_merge_radius_px: int | None = None
                          ) -> list[dict[str, Any]]:
    """The UGR at each threshold, with the same masks, L_b and line of sight."""
    out = []
    for thr in thresholds:
        # The direct threshold varies; reflections keep theirs.
        srcs, _, _ = detection.detect_sources(L, clipped, geom, masks, thr, min_pixels,
                                              merge_radius_px, spectral, lighting,
                                              reflection_threshold, reflection_merge_radius_px)
        ugr_mod.rate_sources(srcs, cam_to_view, ugr_cfg.omega_min_sr, ugr_cfg.omega_max_sr,
                             ugr_cfg.position_index_cap)
        out.append({"threshold_cd_m2": thr, "ugr": ugr_mod.ugr(srcs, L_b),
                    "n_sources": len(srcs)})
    return out
