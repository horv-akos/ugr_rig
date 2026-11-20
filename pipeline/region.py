"""One region (direction) end to end, from a bracket directory on disk:

    bracket -> linearise -> Debevec-weighted merge -> cd/m2 -> sources -> L_b -> UGR
            -> luminance.npy, result.json, sources.csv, and the images of report.images

Runs the same on the Pi straight after capture and on a desk against an
archived bracket, which is how most iterations will happen.
"""

from __future__ import annotations

import csv
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from analysis import background, detection, metrics, ugr as ugr_mod
from hal.camera import BracketManifest
from photometry import calibration, hdr, linearise
from photometry.masks import CameraGeometry, camera_to_view
from photometry.profile import AnalysisConfig

log = logging.getLogger(__name__)

RATIO_WARN = 0.03     # frame-to-frame exposure ratio off by more than this -> flag


# --------------------------------------------------------------------------- #
# merge
# --------------------------------------------------------------------------- #


@dataclass
class Merged:
    hdr: hdr.HDRResult
    manifest: BracketManifest
    weights: tuple[float, float, float]
    weights_source: str
    seconds: float


def merge_bracket(bracket_dir: str | Path, cfg: AnalysisConfig) -> Merged:
    t0 = time.monotonic()
    bracket_dir = Path(bracket_dir)
    manifest = BracketManifest.load(bracket_dir)
    if not manifest.frames:
        raise ValueError(f"{bracket_dir}: bracket has no frames")
    if not manifest.finished_at:
        raise ValueError(f"{bracket_dir}: bracket is incomplete (capture was interrupted)")
    frames = sorted(manifest.frames, key=lambda f: f.t_effective_s)
    merger = hdr.DebevecMerge(noise_floor=cfg.sensor.noise_floor_fraction)
    weights = wsrc = None
    for rec in frames:
        raw = linearise.read_frame(bracket_dir / rec.file, manifest.format)
        if weights is None:
            weights, wsrc = linearise.luminance_weights(raw, cfg.processing.luminance_weights)
        Y, maxfrac = linearise.superpixel(raw, weights, cfg.sensor.saturation_fraction,
                                          cfg.processing.binning)
        del raw
        merger.add(Y, maxfrac, rec.t_effective_s, rec.aperture, rec.iso, rec.shutter_label)
    return Merged(hdr=merger.result(), manifest=manifest, weights=weights,
                  weights_source=wsrc, seconds=time.monotonic() - t0)


def luminance(merged: Merged, cfg: AnalysisConfig, k: float | None = None
              ) -> tuple[np.ndarray, float, bool]:
    """-> (L in cd/m2, k used, vignetting applied)."""
    k = cfg.calibration.k if k is None else k
    if k <= 0:
        raise ValueError("no calibration constant: run `ugrctl calibrate` and put k in "
                         "config/analysis.toml, or pass --k")
    vig = calibration.load_vignette(cfg.lens.vignetting_map, merged.hdr.rel.shape)
    return calibration.apply(merged.hdr.rel, k, vig), k, vig is not None


# --------------------------------------------------------------------------- #
# region
# --------------------------------------------------------------------------- #


@dataclass
class RegionResult:
    name: str
    pan_deg: float
    tilt_deg: float
    ugr: float | None
    category: str
    L_b: float
    background: dict[str, Any]
    sources: list[detection.Source]
    flags: list[str] = field(default_factory=list)
    hdr_stats: dict[str, Any] = field(default_factory=dict)
    detection_info: dict[str, Any] = field(default_factory=dict)
    geometry: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)
    timing_s: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        u = "n/a" if self.ugr is None else f"{self.ugr:.1f}"
        return (f"{self.name}: UGR {u} ({self.category}), L_b {self.L_b:.2f} cd/m2, "
                f"{len(self.sources)} source(s)")


def evaluate_region(bracket_dir: str | Path, out_dir: str | Path, cfg: AnalysisConfig,
                    masks: detection.RegionMasks | None = None,
                    lb: dict[str, Any] | None = None, k: float | None = None,
                    name: str | None = None, images: bool = True,
                    study: str | None = None) -> RegionResult:
    """`lb`: session background from a sweep (`background_from_sweep`), or None for
    the provisional frame mean. `study` only labels the images' stamp."""
    t0 = time.monotonic()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    masks = masks or detection.RegionMasks()

    merged = merge_bracket(bracket_dir, cfg)
    L, k_used, vignetted = luminance(merged, cfg, k)
    t_merge = time.monotonic() - t0

    meta = merged.manifest.meta
    pan, tilt = float(meta.get("pan_deg", 0.0)), float(meta.get("tilt_deg", 0.0))
    name = name or str(meta.get("region", Path(bracket_dir).name))

    geom = CameraGeometry.from_config(cfg, L.shape)
    sources, labels, det_info = detection.detect_sources(
        L, merged.hdr.clipped, geom, masks, cfg.detection.threshold_cd_m2,
        cfg.detection.min_pixels, cfg.detection.merge_radius_px, cfg.calibration.factor)
    exclude = detection.exclusion_mask(masks, L.shape)

    if lb is None:
        L_b, lb_info = background.lb_frame(L, geom.solid_angle, labels, exclude,
                                           cfg.background.windows_in_background)
    else:
        L_b, lb_info = float(lb["L_b"]), dict(lb)

    R = camera_to_view(pan, tilt, cfg.ugr.line_of_sight)
    ugr_mod.rate_sources(sources, R, cfg.ugr.omega_min_sr, cfg.ugr.omega_max_sr,
                         cfg.ugr.position_index_cap)
    value = ugr_mod.ugr(sources, L_b)
    metrics.rate_extras(sources, L_b)
    pos_metrics = metrics.position_metrics(L, geom.solid_angle, exclude,
                                           cfg.detection.threshold_cd_m2, sources)
    pos_metrics["threshold_sensitivity"] = metrics.threshold_sensitivity(
        L, merged.hdr.clipped, geom, masks, cfg.detection.sensitivity_cd_m2,
        cfg.detection.min_pixels, cfg.detection.merge_radius_px, cfg.calibration.factor, R,
        cfg.ugr, L_b)

    flags = []
    if lb_info.get("provisional"):
        flags.append(f"L_b provisional ({lb_info['method']})")
    if not vignetted:
        flags.append("no vignetting correction")
    st = merged.hdr.stats()
    if st["clipped_px"]:
        flags.append(f"{st['clipped_px']} px clipped even at the shortest exposure; "
                     "those luminances are lower bounds")
    for pr in st["pair_ratios"]:
        if pr["ratio"] is not None and abs(pr["ratio"] - 1) > RATIO_WARN:
            flags.append(f"exposure step {pr['pair'][0]} -> {pr['pair'][1]} off by "
                         f"{100 * (pr['ratio'] - 1):+.1f} % (shutter timing?)")
    if merged.weights_source != "matrix":
        flags.append(f"luminance weights from {merged.weights_source}")
    if masks.glazing:
        flags.append("glazing treated as excluded: reflections in it are not counted yet "
                     "(reflection detection, REQUIREMENTS §3.2, is not built)")
    if any(s.flags for s in sources):
        flags.append("some sources carry flags - see the source table")

    result = RegionResult(
        name=name, pan_deg=pan, tilt_deg=tilt, ugr=value, category=ugr_mod.category(value),
        L_b=L_b, background=lb_info, sources=sources, flags=flags, hdr_stats=st,
        detection_info=det_info, geometry=geom.summary(), metrics=pos_metrics,
        provenance={
            "bracket_dir": str(bracket_dir),
            "frames": [{"file": f.file, "shutter": f.shutter_label, "t_s": f.t_effective_s,
                        "sha256": f.sha256} for f in merged.manifest.frames],
            "camera": merged.manifest.camera,
            "capture_settings": merged.manifest.settings,
            "k": k_used,
            "luminance_weights": list(merged.weights),
            "luminance_weights_source": merged.weights_source,
            "line_of_sight": cfg.ugr.line_of_sight,
            "masks": masks.as_dict(),
            "analysis_config": cfg.snapshot(),
            "processed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        },
    )

    np.save(out / "luminance.npy", L)
    result.outputs["luminance"] = "luminance.npy"
    write_sources_csv(out / "sources.csv", sources)
    result.outputs["sources"] = "sources.csv"
    t_analysis = time.monotonic() - t0 - t_merge
    if images:
        from report import images as report_images

        photo = report_images.photo_from_bracket(Path(bracket_dir), merged.manifest.frames,
                                                 pos_metrics["L50_cd_m2"], k_used)
        result.outputs.update(report_images.render_all(
            out, L, labels, sources, masks, exclude, merged.hdr.clipped, geom, R,
            stamp={"ugr": value, "category": result.category, "position": name,
                   "study": study, "n_flags": len(flags)},
            scale=cfg.report.luminance_scale_cd_m2, photo=photo,
            p_cap=cfg.ugr.position_index_cap))
    result.timing_s = {"merge": t_merge, "analysis": t_analysis,
                       "total": time.monotonic() - t0}
    (out / "result.json").write_text(json.dumps(result.as_dict(), indent=2, default=_json))
    return result


def write_sources_csv(path: Path, sources) -> None:
    cols = ["id", "name", "kind", "source_type", "L_cd_m2", "L_peak_cd_m2", "omega_sr",
            "sigma_deg", "tau_deg", "p", "contribution", "n_px", "centroid_x", "centroid_y",
            "clipped_fraction", "spectral_factor", "L_median_cd_m2", "share", "delta_ugr",
            "e_eye_lx", "contrast", "flags"]
    with open(path, "w", newline="") as fh:
        wr = csv.writer(fh)
        wr.writerow(cols)
        for s in sources:
            wr.writerow([s.id, s.name, s.kind, s.source_type or "", f"{s.L_cd_m2:.6g}",
                         f"{s.L_peak_cd_m2:.6g}", f"{s.omega_sr:.6g}", f"{s.sigma_deg:.3f}",
                         f"{s.tau_deg:.3f}", f"{s.p:.4f}", f"{s.contribution:.6g}", s.n_px,
                         f"{s.centroid_px[0]:.1f}", f"{s.centroid_px[1]:.1f}",
                         f"{s.clipped_fraction:.4f}", s.spectral_factor,
                         f"{s.L_median_cd_m2:.6g}", f"{s.share:.4f}",
                         "" if s.delta_ugr is None else f"{s.delta_ugr:.3f}",
                         f"{s.e_eye_lx:.4g}", f"{s.contrast:.4g}", "; ".join(s.flags)])


def _json(o):
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, Path):
        return str(o)
    raise TypeError(type(o))


# --------------------------------------------------------------------------- #
# session-level helpers
# --------------------------------------------------------------------------- #


def calibrate(bracket_dir: str | Path, cfg: AnalysisConfig,
              roi: tuple[float, float, float, float], target_cd_m2: float
              ) -> calibration.KMeasurement:
    merged = merge_bracket(bracket_dir, cfg)
    vig = calibration.load_vignette(cfg.lens.vignetting_map, merged.hdr.rel.shape)
    return calibration.measure_k(merged.hdr.rel, merged.hdr.clipped, roi, target_cd_m2,
                                 vig, cfg.calibration.k, cfg.calibration.k_drift_warn)


def background_from_sweep(bracket_dirs: list[str | Path], cfg: AnalysisConfig,
                          k: float | None = None) -> dict[str, Any]:
    """Hemisphere sweep -> E_ind and L_b for the session.

    Each bracket's manifest must carry the pan/tilt it was shot at. Everything at
    or above the detection threshold is treated as a source and left out. A
    `masks.json` inside a bracket directory marks its windows; with
    `windows_in_background` they are put back in, however bright.
    The hemisphere is centred on the configured line of sight, or straight ahead
    (pan 0, tilt 0) when that is "camera".
    """
    sweep = background.HemisphereSweep(cfg.background.sweep_bin_deg)
    los = cfg.ugr.line_of_sight if cfg.ugr.line_of_sight != "camera" else (0.0, 0.0)
    for d in bracket_dirs:
        merged = merge_bracket(d, cfg)
        L, _, _ = luminance(merged, cfg, k)
        meta = merged.manifest.meta
        geom = CameraGeometry.from_config(cfg, L.shape)
        R = camera_to_view(float(meta.get("pan_deg", 0)), float(meta.get("tilt_deg", 0)), los)
        valid = L < cfg.detection.threshold_cd_m2
        mpath = Path(d) / "masks.json"
        if mpath.exists():
            exclude = detection.exclusion_mask(detection.RegionMasks.load(mpath), L.shape)
            valid = (valid | exclude) if cfg.background.windows_in_background else (valid & ~exclude)
        sweep.add(L, geom, R, valid)
    return sweep.result()
