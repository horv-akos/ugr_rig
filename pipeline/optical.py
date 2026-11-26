"""Optical zero, drift check, aim check and lens tracking: frames in, angles out.

The service moves the head and grabs live-view frames; everything that turns those frames
into angles is here, so it runs on a desk against stored frames like the rest of pipeline/.

    zero        at (0, 0) the fiducial should sit on the optical axis. Its centroid, through
                the lens geometry, is its direction in mount angles; the datum moves by
                minus that, so the fiducial is (0, 0) again (docs/PIVOT_DETAIL.md §5).
    drift       at the end of a session the same fiducial measured again without correcting:
                what is left is what the head lost (steps, slip, a knock). Above
                `drift_limit_deg` every angle of the session is suspect.
    aim         at each position a frame against the position's reference image: the shift
                in degrees is how far replay landed from where the position was saved.
    lens        the fiducial tracked over a grid of poses (photometry.lensfit).

Thresholds live in config/mount.toml [optical].
"""

from __future__ import annotations

import math
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from analysis.spot import Spot, SpotNotFound, find_spot, gray, phase_correlate
from photometry.masks import CameraGeometry, rotation

DEFAULT_MOUNT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "mount.toml"


@dataclass(frozen=True)
class OpticalConfig:
    search_deg: float = 10.0        # the fiducial must be within this of where it is expected
    max_spot_deg: float = 3.0       # larger than this is not the fiducial
    min_contrast: float = 40.0      # grey levels above the window's median
    frames: int = 3                 # frames per measurement, after one discarded
    drift_limit_deg: float = 0.1
    aim_limit_deg: float = 0.2
    aim_min_peak: float = 0.05      # correlation peak below this: the check is inconclusive
    pupil_offset_mm: float = 0.0    # entrance pupil in front of the axes; 0 = not measured

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_optical_config(path: str | Path | None = None) -> OpticalConfig:
    with open(Path(path) if path else DEFAULT_MOUNT_CONFIG, "rb") as fh:
        raw = tomllib.load(fh).get("optical", {})
    known = OpticalConfig.__dataclass_fields__
    unknown = set(raw) - set(known)
    if unknown:
        raise ValueError(f"unknown keys in [optical]: {sorted(unknown)}")
    cfg = OpticalConfig(**{k: (int(v) if k == "frames" else float(v)) for k, v in raw.items()})
    if cfg.frames < 1 or cfg.search_deg <= 0 or cfg.drift_limit_deg <= 0:
        raise ValueError("[optical]: frames >= 1, search_deg and drift_limit_deg > 0")
    return cfg


def frame_geometry(shape: tuple[int, int], sensor_width_mm: float, focal_mm: float,
                   k1: float) -> CameraGeometry:
    h, w = shape
    return CameraGeometry.for_frame(w, h, sensor_width_mm, focal_mm, k1)


def deg_per_px(geom: CameraGeometry) -> float:
    return math.degrees(geom.pitch_mm / geom.focal_mm)


def mount_angles(geom: CameraGeometry, u: float, v: float, pan: float, tilt: float
                 ) -> tuple[float, float]:
    """The direction seen at pixel (u, v) with the head at (pan, tilt), in mount angles."""
    d = rotation(pan, tilt) @ geom.pixel_directions(np.array(u), np.array(v))
    return (math.degrees(math.atan2(float(d[0]), float(d[2]))),
            math.degrees(math.asin(max(-1.0, min(1.0, float(d[1]))))))


def expected_pixel(geom: CameraGeometry, target: tuple[float, float], pan: float, tilt: float
                   ) -> tuple[float, float]:
    """Where a direction (mount angles) should appear with the head at (pan, tilt)."""
    d = rotation(pan, tilt).T @ rotation(*target)[:, 2]
    if d[2] <= 0:
        raise SpotNotFound("the expected spot is behind the camera")
    u, v = geom.project(d)
    return float(u), float(v)


@dataclass
class FiducialMeasurement:
    pan_deg: float           # the fiducial's direction, mount angles
    tilt_deg: float
    offset_deg: float        # its angular distance from where it was expected
    u: float
    v: float
    spread_px: float         # largest centroid difference between the frames
    spots: list[dict]
    head: tuple[float, float]

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for k in ("pan_deg", "tilt_deg", "offset_deg", "u", "v", "spread_px"):
            d[k] = round(d[k], 5)
        return d


def measure_fiducial(frames: list[bytes | np.ndarray], geom: CameraGeometry,
                     head: tuple[float, float], cfg: OpticalConfig,
                     expect: tuple[float, float] = (0.0, 0.0)) -> FiducialMeasurement:
    """The fiducial in each frame, near where direction `expect` should appear with the head
    at `head`. The median centroid of the frames gives its direction."""
    if not frames:
        raise ValueError("no frames")
    per = deg_per_px(geom)
    spots: list[Spot] = []
    for f in frames:
        img = gray(f)
        if img.shape != (geom.height, geom.width):
            raise ValueError(f"frame is {img.shape}, geometry is {(geom.height, geom.width)}")
        c = expected_pixel(geom, expect, *head)
        spots.append(find_spot(img, c, cfg.search_deg / per, cfg.max_spot_deg / per,
                               cfg.min_contrast))
    u = float(np.median([s.u for s in spots]))
    v = float(np.median([s.v for s in spots]))
    spread = float(max(np.hypot(s.u - u, s.v - v) for s in spots)) * 2
    pan, tilt = mount_angles(geom, u, v, *head)
    off = math.degrees(math.acos(max(-1.0, min(1.0, float(
        rotation(pan, tilt)[:, 2] @ rotation(*expect)[:, 2])))))
    return FiducialMeasurement(pan_deg=pan, tilt_deg=tilt, offset_deg=off, u=u, v=v,
                               spread_px=spread, spots=[s.as_dict() for s in spots],
                               head=(float(head[0]), float(head[1])))


def aim_offset(reference: bytes | np.ndarray, frame: bytes | np.ndarray,
               geom: CameraGeometry, cfg: OpticalConfig) -> dict[str, Any]:
    """How far the head points from where the reference image was taken, in degrees.

    Content moving left in the frame means the camera points further right: horizontal
    error = -du, vertical = +dv (rows run down), times degrees per pixel, in the camera's own
    frame. At a steep tilt a pan error is partly a roll about the optical axis, which a shift
    does not see: 0.4 deg of pan reads ~0.24 at tilt 45. Small-angle: fine for the tenths of
    a degree this is for."""
    a, b = gray(reference), gray(frame)
    if a.shape != b.shape:
        from PIL import Image

        b = np.asarray(Image.fromarray(b).resize((a.shape[1], a.shape[0]), Image.BILINEAR),
                       np.float32)
    s = phase_correlate(a, b)
    per = math.degrees(geom.pitch_mm / geom.focal_mm) * (geom.width / a.shape[1])
    dx, dy = -s.du * per, s.dv * per
    total = math.hypot(dx, dy)
    ok = s.peak >= cfg.aim_min_peak
    return {"horizontal_deg": round(dx, 4), "vertical_deg": round(dy, 4),
            "total_deg": round(total, 4),
            "peak": round(s.peak, 4), "conclusive": ok,
            "flag": ok and total > cfg.aim_limit_deg}
