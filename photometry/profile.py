"""Analysis configuration: config/analysis.toml -> dataclasses.

`AnalysisConfig.snapshot()` is what gets stored with every result, so a number
can be traced back to the exact threshold, lens constants and calibration that
produced it.
"""

from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_ANALYSIS_CONFIG = Path(__file__).resolve().parents[1] / "config" / "analysis.toml"


@dataclass(frozen=True)
class SensorConfig:
    pixel_pitch_um: float
    saturation_fraction: float
    noise_floor_fraction: float
    width_px: int = 6000     # raw frame; working resolution is this / binning
    height_px: int = 4000


@dataclass(frozen=True)
class LensConfig:
    name: str
    focal_mm: float
    distortion_k1: float
    vignetting_map: str


@dataclass(frozen=True)
class CalibrationConfig:
    k: float
    k_drift_warn: float
    spectral_factor: float
    spectral_factors: dict[str, float] = field(default_factory=dict)
    iso_q: float = 0.65               # ISO 12232 lens factor, for the k estimate without a meter
    iso_saturation_ratio: float = 1.0  # measured saturation ISO / labelled ISO

    def factor(self, source_type: str | None) -> float:
        if source_type and source_type in self.spectral_factors:
            return self.spectral_factors[source_type]
        return self.spectral_factor


@dataclass(frozen=True)
class ProcessingConfig:
    binning: int
    luminance_weights: str | tuple[float, float, float]


@dataclass(frozen=True)
class DetectionConfig:
    threshold_cd_m2: float
    min_pixels: int
    merge_radius_px: int
    sensitivity_cd_m2: tuple[float, ...] = (250.0, 500.0, 1000.0, 2000.0)
    lighting: str = "dark"                     # the study's: glazing searched (dark) or excluded
    reflection_threshold_cd_m2: float = 500.0
    reflection_merge_radius_px: int = 3


@dataclass(frozen=True)
class BackgroundConfig:
    method: str
    windows_in_background: bool
    sweep_bin_deg: float


@dataclass(frozen=True)
class UGRConfig:
    line_of_sight: str | tuple[float, float]
    omega_min_sr: float
    omega_max_sr: float
    position_index_cap: float


@dataclass(frozen=True)
class ForwardConfig:
    coverage_min: float = 0.9            # below: the forward-gaze UGR is a lower bound
    match_tolerance_deg: float = 0.5     # footprints this close are one source
    luminance_mismatch_warn: float = 0.25  # two whole views of one source further apart: flag
    tiling_overlap: float = 0.1
    coverage_bin_deg: float = 1.0


@dataclass(frozen=True)
class ReportConfig:
    luminance_scale_cd_m2: tuple[float, float] = (1.0, 100000.0)


@dataclass(frozen=True)
class AnalysisConfig:
    sensor: SensorConfig
    lens: LensConfig
    calibration: CalibrationConfig
    processing: ProcessingConfig
    detection: DetectionConfig
    background: BackgroundConfig
    ugr: UGRConfig
    report: ReportConfig = ReportConfig()
    forward: ForwardConfig = ForwardConfig()
    source: Path | None = None

    def snapshot(self) -> dict[str, Any]:
        d = asdict(self)
        d["source"] = str(self.source) if self.source else None
        return d


def load_analysis_config(path: str | Path | None = None) -> AnalysisConfig:
    path = Path(path) if path else DEFAULT_ANALYSIS_CONFIG
    with open(path, "rb") as fh:
        raw: dict[str, Any] = tomllib.load(fh)

    s, l, c = raw["sensor"], raw["lens"], raw.get("calibration", {})
    p, d = raw.get("processing", {}), raw.get("detection", {})
    b, u = raw.get("background", {}), raw.get("ugr", {})

    weights = p.get("luminance_weights", "matrix")
    if not isinstance(weights, str):
        if len(weights) != 3:
            raise ValueError("processing.luminance_weights must be 'matrix' or [r, g, b]")
        weights = tuple(float(x) for x in weights)

    los = u.get("line_of_sight", "camera")
    if not isinstance(los, str):
        if len(los) != 2:
            raise ValueError("ugr.line_of_sight must be 'camera' or [pan_deg, tilt_deg]")
        los = (float(los[0]), float(los[1]))
    elif los != "camera":
        raise ValueError("ugr.line_of_sight must be 'camera' or [pan_deg, tilt_deg]")

    method = str(b.get("method", "frame"))
    if method not in ("frame", "sweep"):
        raise ValueError("background.method must be 'frame' or 'sweep'")

    binning = int(p.get("binning", 2))
    if binning < 2 or binning % 2:
        raise ValueError("processing.binning must be an even number >= 2 (Bayer superpixel)")

    cfg = AnalysisConfig(
        sensor=SensorConfig(
            pixel_pitch_um=float(s["pixel_pitch_um"]),
            saturation_fraction=float(s.get("saturation_fraction", 0.92)),
            noise_floor_fraction=float(s.get("noise_floor_fraction", 0.002)),
            width_px=int(s.get("width_px", 6000)),
            height_px=int(s.get("height_px", 4000)),
        ),
        lens=LensConfig(
            name=str(l.get("name", "")),
            focal_mm=float(l["focal_mm"]),
            distortion_k1=float(l.get("distortion_k1", 0.0)),
            vignetting_map=str(l.get("vignetting_map", "")),
        ),
        calibration=CalibrationConfig(
            k=float(c.get("k", 0.0)),
            k_drift_warn=float(c.get("k_drift_warn", 0.10)),
            spectral_factor=float(c.get("spectral_factor", 1.0)),
            spectral_factors={str(k): float(v)
                              for k, v in c.get("spectral_factors", {}).items()},
            iso_q=float(c.get("iso_q", 0.65)),
            iso_saturation_ratio=float(c.get("iso_saturation_ratio", 1.0)),
        ),
        processing=ProcessingConfig(binning=binning, luminance_weights=weights),
        detection=DetectionConfig(
            threshold_cd_m2=float(d.get("threshold_cd_m2", 500.0)),
            min_pixels=int(d.get("min_pixels", 4)),
            merge_radius_px=int(d.get("merge_radius_px", 3)),
            sensitivity_cd_m2=tuple(float(x) for x in
                                    d.get("sensitivity_cd_m2", (250.0, 500.0, 1000.0, 2000.0))),
            lighting=str(d.get("lighting", "dark")),
            reflection_threshold_cd_m2=float(d.get("reflection_threshold_cd_m2",
                                                   d.get("threshold_cd_m2", 500.0))),
            reflection_merge_radius_px=int(d.get("reflection_merge_radius_px",
                                                 d.get("merge_radius_px", 3))),
        ),
        background=BackgroundConfig(
            method=method,
            windows_in_background=bool(b.get("windows_in_background", True)),
            sweep_bin_deg=float(b.get("sweep_bin_deg", 2.0)),
        ),
        ugr=UGRConfig(
            line_of_sight=los,
            omega_min_sr=float(u.get("omega_min_sr", 3e-4)),
            omega_max_sr=float(u.get("omega_max_sr", 0.1)),
            position_index_cap=float(u.get("position_index_cap", 16.0)),
        ),
        report=ReportConfig(luminance_scale_cd_m2=tuple(
            float(x) for x in raw.get("report", {}).get("luminance_scale_cd_m2", (1.0, 1e5)))),
        forward=ForwardConfig(**{k: float(v) for k, v in raw.get("forward", {}).items()}),
        source=path,
    )
    return cfg
