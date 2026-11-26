"""Glare source extraction from a calibrated luminance map.

Order of precedence, per REQUIREMENTS §3.2:
  1. Exclusion masks - never a source. In `daylight` studies glazing is excluded
     too: a reflection cannot be told from the bright scene behind the glass.
  2. Inclusion ROIs - each known luminaire is drawn once per vehicle and
     direction. Pixels inside it at or above the threshold form ONE named
     source, however many blobs they break into. The ROI area is then closed
     to blob detection. An ROI with `reflects` is that luminaire's reflection:
     path "reflection", the reflection threshold, and the parent's source type
     (glass reflects close to neutrally, so the spectral factor carries over).
  3. Glazing, in `dark` studies: reflections of the cab's own lights. Glass
     reflects a few percent head-on and much more at the grazing angles of a
     raked windscreen, so they get their own, lower threshold, and their own
     merge radius: laminated glass gives a double image (one per surface) that
     must count as one reflection.
  4. Everything else at or above the absolute threshold seeds a source.
     Seeds closer than `merge_radius_px` are one luminaire (LED arrays). Each
     grows to the whole luminaire (`_grown`): a panel lit at 450 cd/m2 with a
     corner at 550 is one source, not a 550 cd/m2 sliver; a lamp does not take
     in its lit surroundings. Sources under `min_pixels` are dropped as hot
     pixels. Reflections in the glazing (3) grow the same way.

A source whose pixels touch the frame edge is cut: its omega is truncated. It is
flagged; the forward-gaze rating replaces it with a whole view from another
position where there is one (analysis.forward).

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


LIGHTING = ("dark", "daylight")


@dataclass
class InclusionROI:
    name: str
    polygon: list[tuple[float, float]]
    source_type: str | None = None
    threshold_cd_m2: float | None = None
    reflects: str | None = None        # name of the luminaire ROI this one is a reflection of


@dataclass
class RegionMasks:
    exclude: list[list[tuple[float, float]]] = field(default_factory=list)
    include: list[InclusionROI] = field(default_factory=list)
    version: str = ""
    glazing: list[list[tuple[float, float]]] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path | None) -> "RegionMasks":
        if not path:
            return cls()
        raw = json.loads(Path(path).read_text())
        return cls(
            exclude=[[tuple(p) for p in poly] for poly in raw.get("exclude", [])],
            glazing=[[tuple(p) for p in poly] for poly in raw.get("glazing", [])],
            include=[InclusionROI(name=r["name"], polygon=[tuple(p) for p in r["polygon"]],
                                  source_type=r.get("type"),
                                  threshold_cd_m2=r.get("threshold_cd_m2"),
                                  reflects=r.get("reflects") or None)
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


def exclusion_mask(masks: RegionMasks, shape: tuple[int, int],
                   lighting: str = "dark") -> np.ndarray:
    """Pixels that can never be a source: exclusion polygons, and glazing in `daylight`
    studies. Glazing that is not a source counts toward L_b in both (`windows_in_background`)."""
    if lighting not in LIGHTING:
        raise ValueError(f"lighting must be one of {LIGHTING}")
    m = np.zeros(shape, bool)
    for poly in masks.exclude + (masks.glazing if lighting == "daylight" else []):
        m |= rasterise(poly, shape)
    return m


def glazing_mask(masks: RegionMasks, shape: tuple[int, int]) -> np.ndarray:
    m = np.zeros(shape, bool)
    for poly in masks.glazing:
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
    path: str = "direct"              # "direct" | "reflection" (in the glazing)
    reflects: str | None = None       # the luminaire a reflection ROI mirrors
    cut: bool = False                 # touches the frame edge: omega truncated
    n_seed_px: int = 0                # blobs: pixels at or above the threshold it grew from
    # filled in by analysis.ugr
    sigma_deg: float = float("nan")
    tau_deg: float = float("nan")
    p: float = float("nan")
    contribution: float = 0.0
    flags: list[str] = field(default_factory=list)
    # filled in by analysis.metrics
    L_median_cd_m2: float = float("nan")
    share: float = float("nan")          # of the position's sum of L^2 omega / p^2
    delta_ugr: float | None = None       # UGR with this source minus UGR without it
    e_eye_lx: float = float("nan")       # direct illuminance at the eye, plane facing the gaze
    contrast: float = float("nan")       # L / L_b


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
    rows, cols = L.shape
    cut = bool(uu.min() == 0 or vv.min() == 0 or uu.max() == cols - 1 or vv.max() == rows - 1)
    if cut:
        flags.append("cut by the frame edge: omega is truncated")
    return Source(cut=cut,
        id=idx, name=name, kind=kind, source_type=source_type, n_px=int(vv.size),
        omega_sr=om, L_cd_m2=float((lum * w).sum() / om) * factor,
        L_peak_cd_m2=float(lum.max()) * factor, spectral_factor=factor,
        L_median_cd_m2=float(np.median(lum)) * factor,
        centroid_px=(cx, cy),
        bbox_px=(int(uu.min()), int(vv.min()), int(uu.max()), int(vv.max())),
        direction_cam=tuple(float(x) for x in d), clipped_fraction=clip_frac, flags=flags,
    )


def _blobs(L: np.ndarray, candidates: np.ndarray, min_pixels: int, merge_radius_px: int
           ) -> tuple[list[tuple[np.ndarray, np.ndarray]], int]:
    """Connected blobs of `candidates`, those closer than `merge_radius_px` joined, brightest
    first; and how many were dropped as smaller than `min_pixels`."""
    if not candidates.any():
        return [], 0
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
    out, dropped = [], 0
    for comp in sorted(range(1, n + 1), key=lambda i: -peaks[i - 1]):
        if counts[comp] < min_pixels:
            dropped += 1
            continue
        sl = slices[comp - 1]
        vv, uu = np.nonzero(lab[sl] == comp)
        out.append((vv + sl[0].start, uu + sl[1].start))
    return out, dropped


EIGHT = np.ones((3, 3), bool)     # pixels touching at an edge or a corner are connected


def _grown(L: np.ndarray, omega: np.ndarray, allowed: np.ndarray, threshold: float,
           min_pixels: int, merge_radius_px: int, extent_fraction: float,
           source_fraction: float) -> tuple[list[tuple[np.ndarray, np.ndarray, int]], int]:
    """Sources seeded at `threshold` and grown to the whole luminaire, brightest first:
    (rows, cols, seed pixels in the source) each, and how many were dropped as too small.

    Seeds are the pixels at or above the threshold, grouped as `_blobs` groups them. Each
    group's extent level is the higher of `extent_fraction` x threshold (fixed, so sessions
    compare) and `source_fraction` x the group's own solid-angle-weighted luminance (so a
    bright lamp does not take in its lit surroundings or the lens's glow, which would dilute
    its mean). The source is every pixel at or above that level, inside `allowed`, connected
    (8 neighbours) to a seed pixel at or above it. A panel straddling the threshold counts
    whole; one wholly under it still does not count. Seed pixels under a bright source's
    level are left out of it: they are its surroundings. One pass, no iteration.
    extent_fraction = 1 and source_fraction = 0 give the plain threshold blobs.
    `min_pixels` applies to the grown source, so a hot pixel stays dropped."""
    groups, _ = _blobs(L, (L >= threshold) & allowed, 1, merge_radius_px)
    if not groups:
        return [], 0
    floor = extent_fraction * threshold
    # Any extent at a level >= floor lies inside a component of this: crop each group to it.
    outer, _ = ndimage.label((L >= floor) & allowed, structure=EIGHT)
    boxes = ndimage.find_objects(outer)
    taken = np.zeros(L.shape, bool)
    out, dropped = [], 0
    for vv, uu in groups:
        free = ~taken[vv, uu]
        vv, uu = vv[free], uu[free]
        if vv.size == 0:
            continue                               # swallowed by a brighter source
        w = omega[vv, uu].astype(np.float64)
        level = max(floor, source_fraction * float((L[vv, uu] * w).sum() / w.sum()))
        keep = L[vv, uu] >= level
        vv, uu = vv[keep], uu[keep]
        comps = np.unique(outer[vv, uu])
        r0 = min(boxes[c - 1][0].start for c in comps)
        r1 = max(boxes[c - 1][0].stop for c in comps)
        c0 = min(boxes[c - 1][1].start for c in comps)
        c1 = max(boxes[c - 1][1].stop for c in comps)
        crop = (slice(r0, r1), slice(c0, c1))
        cand = np.isin(outer[crop], comps) & (L[crop] >= level) & ~taken[crop]
        lab, _ = ndimage.label(cand, structure=EIGHT)
        hit = np.unique(lab[vv - r0, uu - c0])
        member = np.isin(lab, hit[hit > 0])
        if int(member.sum()) < min_pixels:
            dropped += 1
            continue
        taken[crop] |= member
        mv, mu = np.nonzero(member)
        out.append((mv + r0, mu + c0, int(vv.size)))
    return out, dropped


def detect_sources(L: np.ndarray, clipped: np.ndarray, geom: CameraGeometry,
                   masks: RegionMasks, threshold: float, min_pixels: int,
                   merge_radius_px: int, spectral, lighting: str = "dark",
                   reflection_threshold: float | None = None,
                   reflection_merge_radius_px: int | None = None, *,
                   extent_fraction: float = 0.5, source_fraction: float = 2 / 3,
                   extent_flag_ratio: float = 10.0
                   ) -> tuple[list[Source], np.ndarray, dict]:
    """Return (sources, label image, info). Label 0 = no source, i = sources[i-1].

    `spectral` maps a source type (or None) to its spectral correction factor. In `dark`
    studies glazing is searched for reflections against `reflection_threshold` (default:
    the direct threshold). Blobs (not ROIs) grow from their threshold to the whole luminaire
    (`_grown`); one grown to more than `extent_flag_ratio` times its seed is flagged
    (0 = never)."""
    if not 0.0 < extent_fraction <= 1.0 or not 0.0 <= source_fraction < 1.0:
        raise ValueError("extent_fraction must be in (0, 1], source_fraction in [0, 1)")
    shape = L.shape
    omega = geom.solid_angle
    r_thr = threshold if reflection_threshold is None else reflection_threshold
    r_merge = merge_radius_px if reflection_merge_radius_px is None \
        else reflection_merge_radius_px
    exclude = exclusion_mask(masks, shape, lighting)
    glazing = glazing_mask(masks, shape) & ~exclude if lighting == "dark" else \
        np.zeros(shape, bool)
    labels = np.zeros(shape, np.int32)
    sources: list[Source] = []
    roi_area = np.zeros(shape, bool)
    types = {r.name: r.source_type for r in masks.include}

    for roi in masks.include:
        area = rasterise(roi.polygon, shape) & ~exclude
        roi_area |= area
        reflection = roi.reflects is not None
        thr = roi.threshold_cd_m2 if roi.threshold_cd_m2 is not None \
            else (r_thr if reflection else threshold)
        stype = roi.source_type or (types.get(roi.reflects) if reflection else None)
        member = area & (L >= thr) & (labels == 0)
        if not member.any():
            continue
        idx = len(sources) + 1
        labels[member] = idx
        vv, uu = np.nonzero(member)
        s = _describe(idx, roi.name, "roi", stype, vv, uu, L, omega, clipped, geom,
                      spectral(stype))
        if reflection:
            s.path, s.reflects = "reflection", roi.reflects
        sources.append(s)

    def blob(idx: int, name: str, vv, uu, n_seed: int, thr: float) -> Source:
        s = _describe(idx, name, "blob", None, vv, uu, L, omega, clipped, geom,
                      spectral(None))
        s.n_seed_px = n_seed
        if extent_flag_ratio > 0 and vv.size > extent_flag_ratio * n_seed:
            s.flags.append(f"grown from {n_seed} px at or above {thr:g} cd/m2 to {vv.size} px: "
                           "mostly under the threshold. Check it is one luminaire, not its lit "
                           "surroundings or the lens's glow (an inclusion or exclusion area "
                           "settles it)")
        return s

    dropped = 0
    n_refl = 0
    if glazing.any():
        found, d = _grown(L, omega, glazing & ~roi_area & (labels == 0), r_thr, min_pixels,
                          r_merge, extent_fraction, source_fraction)
        dropped += d
        for vv, uu, n_seed in found:
            idx = len(sources) + 1
            n_refl += 1
            labels[vv, uu] = idx
            s = blob(idx, f"R{n_refl}", vv, uu, n_seed, r_thr)
            s.path = "reflection"
            sources.append(s)

    found, d = _grown(L, omega, ~exclude & ~roi_area & ~glazing & (labels == 0), threshold,
                      min_pixels, merge_radius_px, extent_fraction, source_fraction)
    dropped += d
    for vv, uu, n_seed in found:
        idx = len(sources) + 1
        labels[vv, uu] = idx
        sources.append(blob(idx, f"S{idx}", vv, uu, n_seed, threshold))

    info = {"threshold_cd_m2": threshold, "min_pixels": min_pixels,
            "merge_radius_px": merge_radius_px, "dropped_small_blobs": dropped,
            "extent_fraction": extent_fraction, "source_fraction": source_fraction,
            "extent_flag_ratio": extent_flag_ratio,
            "lighting": lighting, "reflection_threshold_cd_m2": r_thr,
            "reflection_merge_radius_px": r_merge,
            "glazing": "searched for reflections" if lighting == "dark" else "excluded",
            "n_reflections": sum(s.path == "reflection" for s in sources),
            "excluded_px": int(exclude.sum()), "n_glazing": len(masks.glazing),
            "n_roi": len(masks.include),
            "masks_version": masks.version}
    return sources, labels, info
