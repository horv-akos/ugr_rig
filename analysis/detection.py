"""Glare source extraction from a calibrated luminance map.

Order of precedence, per REQUIREMENTS §3.2:
  1. Exclusion masks (windows) - never a source.
  2. Inclusion ROIs - each known luminaire is drawn once per vehicle and
     direction. Pixels inside it at or above the threshold form ONE named
     source, however many blobs they break into. The ROI area is then closed
     to blob detection.
  3. Everything else at or above the absolute threshold: connected blobs.
     Blobs closer than `merge_radius_px` are one luminaire (LED arrays), and
     blobs under `min_pixels` are dropped as hot pixels.

ROI and mask polygons are in normalised image coordinates (0..1, origin top
left), so they survive a change of binning.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from scipy import ndimage

from photometry.masks import CameraGeometry


# --------------------------------------------------------------------------- #
# region masks (per vehicle and direction)
# --------------------------------------------------------------------------- #


@dataclass
class InclusionROI:
    name: str
    polygon: list[tuple[float, float]]
    source_type: str | None = None
    threshold_cd_m2: float | None = None


@dataclass
class RegionMasks:
    exclude: list[list[tuple[float, float]]] = field(default_factory=list)
    include: list[InclusionROI] = field(default_factory=list)
    version: str = ""

    @classmethod
    def load(cls, path: str | Path | None) -> "RegionMasks":
        if not path:
            return cls()
        raw = json.loads(Path(path).read_text())
        return cls(
            exclude=[[tuple(p) for p in poly] for poly in raw.get("exclude", [])],
            include=[InclusionROI(name=r["name"], polygon=[tuple(p) for p in r["polygon"]],
                                  source_type=r.get("type"),
                                  threshold_cd_m2=r.get("threshold_cd_m2"))
                     for r in raw.get("include", [])],
            version=str(raw.get("version", Path(path).name)),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def rasterise(polygon: list[tuple[float, float]], shape: tuple[int, int]) -> np.ndarray:
    from PIL import Image, ImageDraw

    h, w = shape
    img = Image.new("1", (w, h), 0)
    ImageDraw.Draw(img).polygon([(x * w, y * h) for x, y in polygon], fill=1, outline=1)
    return np.array(img, dtype=bool)


def exclusion_mask(masks: RegionMasks, shape: tuple[int, int]) -> np.ndarray:
    m = np.zeros(shape, bool)
    for poly in masks.exclude:
        m |= rasterise(poly, shape)
    return m


# --------------------------------------------------------------------------- #
# sources
# --------------------------------------------------------------------------- #


@dataclass
class Source:
    id: int
    name: str
    kind: str                         # "roi" | "blob"
    source_type: str | None
    n_px: int
    omega_sr: float
    L_cd_m2: float                    # solid-angle-weighted mean, spectral factor applied
    L_peak_cd_m2: float
    spectral_factor: float
    centroid_px: tuple[float, float]  # (x, y) at working resolution
    bbox_px: tuple[int, int, int, int]
    direction_cam: tuple[float, float, float]
    clipped_fraction: float
    # filled in by analysis.ugr
    sigma_deg: float = float("nan")
    tau_deg: float = float("nan")
    p: float = float("nan")
    contribution: float = 0.0
    flags: list[str] = field(default_factory=list)


def _describe(idx: int, name: str, kind: str, source_type: str | None,
              vv: np.ndarray, uu: np.ndarray,
              L: np.ndarray, omega: np.ndarray, clipped: np.ndarray,
              geom: CameraGeometry, factor: float) -> Source:
    w = omega[vv, uu].astype(np.float64)
    lum = L[vv, uu].astype(np.float64)
    om = float(w.sum())
    dirs = geom.pixel_directions(uu, vv)
    d = (dirs * w[:, None]).sum(axis=0)
    d /= np.linalg.norm(d)
    cx, cy = float((uu * w).sum() / om), float((vv * w).sum() / om)
    clip_frac = float(clipped[vv, uu].mean())
    flags = ["clipped: luminance is a lower bound"] if clip_frac > 0 else []
    return Source(
        id=idx, name=name, kind=kind, source_type=source_type, n_px=int(vv.size),
        omega_sr=om, L_cd_m2=float((lum * w).sum() / om) * factor,
        L_peak_cd_m2=float(lum.max()) * factor, spectral_factor=factor,
        centroid_px=(cx, cy),
        bbox_px=(int(uu.min()), int(vv.min()), int(uu.max()), int(vv.max())),
        direction_cam=tuple(float(x) for x in d), clipped_fraction=clip_frac, flags=flags,
    )


def detect_sources(L: np.ndarray, clipped: np.ndarray, geom: CameraGeometry,
                   masks: RegionMasks, threshold: float, min_pixels: int,
                   merge_radius_px: int, spectral) -> tuple[list[Source], np.ndarray, dict]:
    """Return (sources, label image, info). Label 0 = no source, i = sources[i-1].

    `spectral` maps a source type (or None) to its spectral correction factor.
    """
    shape = L.shape
    omega = geom.solid_angle
    exclude = exclusion_mask(masks, shape)
    labels = np.zeros(shape, np.int32)
    sources: list[Source] = []
    roi_area = np.zeros(shape, bool)

    for roi in masks.include:
        area = rasterise(roi.polygon, shape) & ~exclude
        roi_area |= area
        thr = roi.threshold_cd_m2 if roi.threshold_cd_m2 is not None else threshold
        member = area & (L >= thr) & (labels == 0)
        if not member.any():
            continue
        idx = len(sources) + 1
        labels[member] = idx
        vv, uu = np.nonzero(member)
        sources.append(_describe(idx, roi.name, "roi", roi.source_type, vv, uu, L, omega,
                                 clipped, geom, spectral(roi.source_type)))

    candidates = (L >= threshold) & ~exclude & ~roi_area
    dropped = 0
    if candidates.any():
        if merge_radius_px > 0:
            grown = ndimage.binary_dilation(
                candidates, structure=ndimage.generate_binary_structure(2, 1),
                iterations=merge_radius_px)
        else:
            grown = candidates
        lab, n = ndimage.label(grown, structure=np.ones((3, 3), bool))
        lab = np.where(candidates, lab, 0)
        counts = np.bincount(lab.ravel(), minlength=n + 1)
        peaks = ndimage.maximum(L, lab, index=np.arange(1, n + 1)) if n else []
        slices = ndimage.find_objects(lab)
        order = sorted(range(1, n + 1), key=lambda i: -peaks[i - 1])  # brightest first
        for comp in order:
            if counts[comp] < min_pixels:
                dropped += 1
                continue
            sl = slices[comp - 1]
            vv, uu = np.nonzero(lab[sl] == comp)
            vv, uu = vv + sl[0].start, uu + sl[1].start
            idx = len(sources) + 1
            labels[vv, uu] = idx
            sources.append(_describe(idx, f"S{idx}", "blob", None, vv, uu, L, omega,
                                     clipped, geom, spectral(None)))

    info = {"threshold_cd_m2": threshold, "min_pixels": min_pixels,
            "merge_radius_px": merge_radius_px, "dropped_small_blobs": dropped,
            "excluded_px": int(exclude.sum()), "n_roi": len(masks.include),
            "masks_version": masks.version}
    return sources, labels, info
