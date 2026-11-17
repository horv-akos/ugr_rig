"""Background luminance L_b.

CIE 117 defines L_b = E_ind / pi, with E_ind the indirect vertical illuminance
at the eye: the whole hemisphere in front of the observer, minus the glare
sources. REQUIREMENTS §3.3 gets it from one coarse hemisphere sweep per session.

Two ways in:
  `lb_frame`         solid-angle-weighted mean of the non-source pixels of one
                     frame. Depends on where the camera points - provisional,
                     and flagged as such in every result that uses it.
  `HemisphereSweep`  accumulates frames from several directions onto a grid
                     around the line of sight and integrates E_ind = sum L cos(theta) dOmega.
                     Where the sweep left gaps, the result is scaled up by the
                     cosine-weighted coverage and the coverage is reported.
"""

from __future__ import annotations

import math

import numpy as np

from photometry.masks import CameraGeometry


def lb_frame(L: np.ndarray, omega: np.ndarray, source_labels: np.ndarray,
             exclude: np.ndarray, windows_in_background: bool) -> tuple[float, dict]:
    valid = source_labels == 0
    if not windows_in_background:
        valid &= ~exclude
    w = omega[valid].astype(np.float64)
    if w.sum() <= 0:
        raise ValueError("no background pixels left in the frame")
    lb = float((L[valid].astype(np.float64) * w).sum() / w.sum())
    return lb, {"method": "frame", "provisional": True, "omega_sr": float(w.sum()),
                "note": "frame mean, not the hemisphere: depends on where the camera points"}


class HemisphereSweep:
    def __init__(self, bin_deg: float = 2.0, stride: int = 4) -> None:
        self.bin = bin_deg
        self.stride = stride
        self.n_theta = int(math.ceil(90.0 / bin_deg))
        self.n_phi = int(math.ceil(360.0 / bin_deg))
        self._lw = np.zeros((self.n_theta, self.n_phi))
        self._w = np.zeros((self.n_theta, self.n_phi))
        self.frames = 0

    def add(self, L: np.ndarray, geom: CameraGeometry, cam_to_view: np.ndarray,
            valid: np.ndarray) -> None:
        """valid: pixels that are background (not sources)."""
        s = self.stride
        vv, uu = np.mgrid[0:L.shape[0]:s, 0:L.shape[1]:s]
        keep = valid[vv, uu]
        vv, uu = vv[keep], uu[keep]
        d = geom.pixel_directions(uu, vv) @ cam_to_view.T
        front = d[:, 2] > 0
        d, vv, uu = d[front], vv[front], uu[front]
        theta = np.degrees(np.arccos(np.clip(d[:, 2], -1, 1)))
        phi = np.degrees(np.arctan2(d[:, 1], d[:, 0])) % 360.0
        ti = np.minimum((theta / self.bin).astype(int), self.n_theta - 1)
        pi_ = np.minimum((phi / self.bin).astype(int), self.n_phi - 1)
        w = geom.solid_angle[vv, uu].astype(np.float64) * s * s
        np.add.at(self._lw, (ti, pi_), L[vv, uu] * w)
        np.add.at(self._w, (ti, pi_), w)
        self.frames += 1

    def result(self) -> dict:
        t = np.radians(np.arange(self.n_theta + 1) * self.bin).clip(0, math.pi / 2)
        # integral of cos(theta) dOmega over each bin
        c = (np.sin(t[1:]) ** 2 - np.sin(t[:-1]) ** 2) / 2.0 * np.radians(self.bin)
        C = np.repeat(c[:, None], self.n_phi, axis=1)
        filled = self._w > 0
        Lbin = np.zeros_like(self._lw)
        Lbin[filled] = self._lw[filled] / self._w[filled]
        e_filled = float((Lbin * C)[filled].sum())
        coverage = float(C[filled].sum() / math.pi)
        if coverage <= 0:
            raise ValueError("sweep covered nothing in front of the line of sight")
        e_ind = e_filled / coverage
        return {"method": "sweep", "provisional": coverage < 0.8, "E_ind_lx": e_ind,
                "L_b": e_ind / math.pi, "coverage_cos_weighted": coverage,
                "frames": self.frames, "bin_deg": self.bin}
