"""Viewing geometry: per-pixel direction and solid angle, mount rotations, and
the Guth position index.

Frames
------
camera   x right, y up, z along the optical axis (image rows run downwards)
cabin    x right, y up, z forward; the camera frame at pan = tilt = 0
view     x right, y up, z along the observer's line of sight

Mount convention: pan positive = camera turns right (seen from above), tilt
positive = camera looks up. `invert` in config/mount.toml must be set so the
head obeys this.

Position index
--------------
Guth, in the analytical form fitted by Levin (1975) - the one in the IES
handbook and in Evalglare - for sources at or above the line of sight; Iwata's
model below it. The two meet at the horizontal plane to within ~10 %.
The brow shields sources overhead, so p rises fastest straight up: a source 30 deg
above the line of sight gets p = 3.3, the same source 30 deg below p = 1.5,
30 deg to the side p = 1.5. Since a source counts as 1/p^2, overhead sources
count least and sources below the line of sight count most.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property

import numpy as np


# --------------------------------------------------------------------------- #
# lens geometry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CameraGeometry:
    width: int               # working resolution
    height: int
    pitch_mm: float          # working pixel pitch = raw pitch * binning
    focal_mm: float
    k1: float = 0.0          # r_true = r_image * (1 + k1 r_image^2), r in units of f
    cx: float | None = None  # principal point in working pixels; None = image centre
    cy: float | None = None

    @classmethod
    def from_config(cls, cfg, shape: tuple[int, int]) -> "CameraGeometry":
        h, w = shape
        return cls(width=w, height=h,
                   pitch_mm=cfg.sensor.pixel_pitch_um * 1e-3 * cfg.processing.binning,
                   focal_mm=cfg.lens.focal_mm, k1=cfg.lens.distortion_k1)

    @classmethod
    def for_frame(cls, width: int, height: int, sensor_width_mm: float, focal_mm: float,
                  k1: float = 0.0) -> "CameraGeometry":
        """A frame of any size that shows the whole sensor width, e.g. a live-view frame."""
        return cls(width=width, height=height, pitch_mm=sensor_width_mm / width,
                   focal_mm=focal_mm, k1=k1)

    def project(self, d: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Camera-frame directions (..., 3) -> pixel coordinates (u, v); the inverse of
        `pixel_directions`. The radial term is inverted by Newton steps."""
        d = np.asarray(d, np.float64)
        xt, yt = d[..., 0] / d[..., 2], d[..., 1] / d[..., 2]
        rt = np.hypot(xt, yt)
        r = rt.copy()
        for _ in range(8):                      # r (1 + k1 r^2) = rt
            r -= (r * (1 + self.k1 * r * r) - rt) / (1 + 3 * self.k1 * r * r)
        s = np.divide(r, rt, out=np.ones_like(rt), where=rt > 0)
        cx = (self.width - 1) / 2 if self.cx is None else self.cx
        cy = (self.height - 1) / 2 if self.cy is None else self.cy
        scale = self.focal_mm / self.pitch_mm
        return cx + xt * s * scale, cy - yt * s * scale

    def _normalised(self, u: np.ndarray, v: np.ndarray):
        cx = (self.width - 1) / 2 if self.cx is None else self.cx
        cy = (self.height - 1) / 2 if self.cy is None else self.cy
        a = (u - cx) * (self.pitch_mm / self.focal_mm)
        b = -(v - cy) * (self.pitch_mm / self.focal_mm)
        r2 = a * a + b * b
        s = 1.0 + self.k1 * r2
        return a * s, b * s, r2, s

    def pixel_directions(self, u: np.ndarray, v: np.ndarray) -> np.ndarray:
        """Unit vectors (..., 3) in the camera frame for pixel coordinates u (col), v (row)."""
        x, y, _, _ = self._normalised(np.asarray(u, np.float64), np.asarray(v, np.float64))
        d = np.stack([x, y, np.ones_like(x)], axis=-1)
        return d / np.linalg.norm(d, axis=-1, keepdims=True)

    @cached_property
    def directions(self) -> np.ndarray:
        v, u = np.mgrid[0:self.height, 0:self.width].astype(np.float32)
        return self.pixel_directions(u, v).astype(np.float32)

    @cached_property
    def solid_angle(self) -> np.ndarray:
        """Steradians per pixel. Rectilinear: dA / f^2 / (1 + r^2)^(3/2), plus the
        Jacobian of the radial distortion term."""
        v, u = np.mgrid[0:self.height, 0:self.width].astype(np.float64)
        x, y, r2, s = self._normalised(u, v)
        jac = s * (1.0 + 3.0 * self.k1 * r2)
        area = (self.pitch_mm / self.focal_mm) ** 2
        return (area * jac / (1.0 + x * x + y * y) ** 1.5).astype(np.float32)

    def fov_deg(self) -> tuple[float, float]:
        hw = self.width * self.pitch_mm / 2
        hh = self.height * self.pitch_mm / 2
        return (2 * np.degrees(np.arctan(hw / self.focal_mm)),
                2 * np.degrees(np.arctan(hh / self.focal_mm)))

    def summary(self) -> dict:
        fh, fv = self.fov_deg()
        return {"width": self.width, "height": self.height, "pitch_mm": self.pitch_mm,
                "focal_mm": self.focal_mm, "k1": self.k1, "fov_h_deg": fh, "fov_v_deg": fv,
                "frame_sr": float(self.solid_angle.sum())}


# --------------------------------------------------------------------------- #
# rotations
# --------------------------------------------------------------------------- #


def rotation(pan_deg: float, tilt_deg: float) -> np.ndarray:
    """3x3 matrix taking camera-frame vectors to the cabin frame."""
    p, t = np.radians(pan_deg), np.radians(tilt_deg)
    z = np.array([np.sin(p) * np.cos(t), np.sin(t), np.cos(p) * np.cos(t)])
    x = np.array([np.cos(p), 0.0, -np.sin(p)])
    y = np.cross(z, x)
    return np.stack([x, y, z], axis=1)


def camera_to_view(cam_pan: float, cam_tilt: float,
                   line_of_sight: str | tuple[float, float]) -> np.ndarray:
    """Matrix taking camera-frame vectors to the observer's view frame."""
    if line_of_sight == "camera":
        return np.eye(3)
    los_pan, los_tilt = line_of_sight
    return rotation(los_pan, los_tilt).T @ rotation(cam_pan, cam_tilt)


# --------------------------------------------------------------------------- #
# position index
# --------------------------------------------------------------------------- #


def guth_levin(sigma_deg, tau_deg):
    """Guth position index, Levin's fit. sigma: angle from the line of sight;
    tau: angle of the source's plane from vertical (0 = straight up, 90 = level)."""
    s, t = np.asarray(sigma_deg, float), np.asarray(tau_deg, float)
    return np.exp((35.2 - 0.31889 * t - 1.22 * np.exp(-2.0 * t / 9.0)) * 1e-3 * s
                  + (21.0 + 0.26667 * t - 0.002963 * t * t) * 1e-5 * s * s)


def iwata(r):
    """Iwata position index below the line of sight; r = tan(sigma), capped at 3."""
    r = np.minimum(np.asarray(r, float), 3.0)
    return 1.0 + np.where(r < 0.6, 0.8, 1.2) * r


def angles(d_view: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(sigma, tau) in degrees for view-frame unit vectors."""
    d = np.asarray(d_view, float)
    x, y, z = d[..., 0], d[..., 1], d[..., 2]
    sigma = np.degrees(np.arccos(np.clip(z, -1.0, 1.0)))
    tau = np.degrees(np.arctan2(np.abs(x), y))     # 0 up, 90 level, 180 down
    return sigma, tau


def position_index(d_view: np.ndarray, cap: float = 16.0) -> np.ndarray:
    d = np.asarray(d_view, float)
    x, y, z = d[..., 0], d[..., 1], d[..., 2]
    sigma, tau = angles(d)
    above = guth_levin(sigma, np.minimum(tau, 90.0))
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.where(z > 0, np.hypot(x, y) / z, np.inf)
    below = iwata(r)
    p = np.minimum(np.where(y >= 0, above, below), cap)
    return np.where(z > 0, p, np.inf)              # behind the observer: no contribution
