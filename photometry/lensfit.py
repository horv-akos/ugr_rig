"""Lens geometry from the head: focal length and radial distortion without a chart.

The head turns the camera by known angles while the camera watches one small spot. Where the
spot lands in the frame against the angle turned is the lens's projection, so a fit of

    pixel = project(focal, k1, R(pan, tilt)^T (P - c(pan, tilt)))

over a grid of poses gives the focal length and k1 (photometry.masks conventions). Every solid
angle scales with 1/focal^2, so 1 % in focal length is 2 % in every source's omega: worth
measuring rather than trusting the number on the lens.

What it rests on:
- The commanded angles. The fit is only as good as the gear ratios (docs/PIVOT_DETAIL.md §8):
  a ratio 1 % off reads as a focal length 1 % off.
- Parallax. The head turns about its axes, not about the lens's entrance pupil. With the pupil
  a distance e in front of the axis and the spot at distance D, the spot's apparent angle runs
  ahead of the head's by about e/D, which reads as a focal length e/D too long. The model
  places the pupil at `pupil_offset_mm` (config/mount.toml) and the spot at `distance_m`; with
  the offset not measured (0), use a spot far away (D > 100 e keeps it under 1 %).
- Live view showing the full sensor width (open item, REQUIREMENTS §9). The principal point is
  held at the frame centre: with pure rotations it is nearly degenerate with the spot's
  direction.
- The zero's tilt. Pan turns about the pan axis, but the zero (set on the fiducial) need not
  look square to it: with the zero looking down by a few degrees, panning sweeps the spot
  along a curve, which a model that assumed a level zero would read as distortion. The fit
  carries the zero's tilt above the plane the pan axis turns in as a fifth parameter; it
  comes out of the curvature of the pan tracks, and is worth knowing on its own (it is the
  fiducial's elevation when the base is level).

The fit also returns the spot's direction at the zero, which is the optical zero's own answer
seen from many poses.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace

import numpy as np

from .masks import CameraGeometry, rotation


@dataclass(frozen=True)
class Sighting:
    pan_deg: float           # commanded pose
    tilt_deg: float
    u: float                 # spot centroid, pixels of the frame
    v: float


@dataclass
class LensFit:
    focal_mm: float
    k1: float
    focal_sd_mm: float
    k1_sd: float
    spot_pan_deg: float      # the spot's direction, mount angles (relative to the zero)
    spot_tilt_deg: float
    zero_tilt_deg: float     # the zero's tilt above the plane the pan axis turns in
    zero_tilt_sd_deg: float
    rms_px: float
    max_px: float
    rms_deg: float
    n: int
    frame: tuple[int, int]   # width, height of the frames used
    sensor_width_mm: float
    distance_m: float
    pupil_offset_mm: float
    residuals: list[list[float]]   # per sighting: pan, tilt, du, dv (pixels)

    def as_dict(self) -> dict:
        return asdict(self)


def predict(geom: CameraGeometry, poses: np.ndarray, spot_pan: float, spot_tilt: float,
            distance_m: float, pupil_offset_mm: float, zero_tilt: float = 0.0
            ) -> tuple[np.ndarray, np.ndarray]:
    """Pixel position of the spot for each commanded (pan, tilt) in `poses` (n, 2). Angles in
    the pan axis's frame: the camera is at rotation(pan, zero_tilt + tilt), the spot at
    (spot_pan, zero_tilt + spot_tilt)."""
    p = rotation(spot_pan, zero_tilt + spot_tilt)[:, 2] * distance_m * 1000.0   # the spot, mm
    us, vs = [], []
    for pan, tilt in poses:
        r = rotation(pan, zero_tilt + tilt)
        c = r @ np.array([0.0, 0.0, pupil_offset_mm])                     # the pupil, mm
        u, v = geom.project(r.T @ (p - c))
        us.append(u)
        vs.append(v)
    return np.array(us, np.float64), np.array(vs, np.float64)


def fit(sightings: list[Sighting], width: int, height: int, sensor_width_mm: float,
        focal_mm: float, distance_m: float, pupil_offset_mm: float = 0.0) -> LensFit:
    """Least squares over focal length, k1, the spot's direction and the zero's tilt.
    `focal_mm` is the start (the nominal value)."""
    from scipy.optimize import least_squares

    if len(sightings) < 6:
        raise ValueError(f"{len(sightings)} sightings: need at least 6 for the lens fit")
    if not distance_m > 0:
        raise ValueError("the spot distance must be positive")
    poses = np.array([[s.pan_deg, s.tilt_deg] for s in sightings], np.float64)
    obs = np.array([[s.u, s.v] for s in sightings], np.float64)
    base = CameraGeometry.for_frame(width, height, sensor_width_mm, focal_mm)

    def resid(x: np.ndarray) -> np.ndarray:
        g = replace(base, focal_mm=float(x[0]), k1=float(x[1]))
        u, v = predict(g, poses, float(x[2]), float(x[3]), distance_m, pupil_offset_mm,
                       float(x[4]))
        return np.concatenate([u - obs[:, 0], v - obs[:, 1]])

    r = least_squares(resid, np.array([focal_mm, 0.0, 0.0, 0.0, 0.0]),
                      x_scale=np.array([focal_mm * 0.01, 0.01, 0.1, 0.1, 1.0]), method="lm")
    res = r.fun
    dof = max(1, res.size - 5)
    s2 = float(res @ res) / dof
    try:
        cov = np.linalg.inv(r.jac.T @ r.jac) * s2
        sd = np.sqrt(np.clip(np.diag(cov), 0, None))
    except np.linalg.LinAlgError:
        sd = np.full(5, np.nan)
    f, k1 = float(r.x[0]), float(r.x[1])
    du, dv = res[: len(poses)], res[len(poses):]
    per_px = sensor_width_mm / width / f                    # radians per pixel, on axis
    rms = float(np.sqrt(np.mean(du ** 2 + dv ** 2)))
    return LensFit(
        focal_mm=f, k1=k1, focal_sd_mm=float(sd[0]), k1_sd=float(sd[1]),
        spot_pan_deg=float(r.x[2]), spot_tilt_deg=float(r.x[3]),
        zero_tilt_deg=float(r.x[4]), zero_tilt_sd_deg=float(sd[4]), rms_px=rms,
        max_px=float(np.max(np.hypot(du, dv))), rms_deg=float(np.degrees(rms * per_px)),
        n=len(poses), frame=(width, height), sensor_width_mm=sensor_width_mm,
        distance_m=distance_m, pupil_offset_mm=pupil_offset_mm,
        residuals=[[float(p), float(t), round(float(a), 3), round(float(b), 3)]
                   for (p, t), a, b in zip(poses, du, dv)])


def grid(fov_deg: tuple[float, float], reach: float = 0.38, n: int = 5
         ) -> list[tuple[float, float]]:
    """Poses around the zero that sweep the spot across most of the frame: n x n, out to
    `reach` of the field of view each way. Corners included: they carry the distortion."""
    ps = np.linspace(-reach, reach, n) * fov_deg[0]
    ts = np.linspace(-reach, reach, n) * fov_deg[1]
    return [(round(float(p), 3), round(float(t), 3)) for t in ts for p in ps]
