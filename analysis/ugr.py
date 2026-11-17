"""CIE 117:1995 Unified Glare Rating.

    UGR = 8 log10( 0.25 / L_b * sum_i L_i^2 omega_i / p_i^2 )

Each source is rated at its solid-angle-weighted centre direction, as one
luminaire, per CIE 117. Sources outside the formula's validity range
(omega < 0.0003 sr or > 0.1 sr) are flagged rather than dropped, so the
number stays comparable between sessions and the flag says how far to trust it.
"""

from __future__ import annotations

import math

import numpy as np

from photometry.masks import angles, position_index

# CIE 117 perception scale (upper edge of each step)
SCALE = [
    (10, "imperceptible"),
    (13, "just perceptible"),
    (16, "perceptible"),
    (19, "just acceptable"),
    (22, "unacceptable"),
    (25, "just uncomfortable"),
    (28, "uncomfortable"),
]


def category(ugr: float | None) -> str:
    if ugr is None:
        return "no glare sources"
    for edge, name in SCALE:
        if ugr <= edge:
            return name
    return "intolerable"


def rate_sources(sources, cam_to_view: np.ndarray, omega_min: float, omega_max: float,
                 p_cap: float) -> None:
    """Fill sigma, tau, p and contribution on each Source in place."""
    for s in sources:
        d = cam_to_view @ np.asarray(s.direction_cam, float)
        sigma, tau = angles(d)
        s.sigma_deg, s.tau_deg = float(sigma), float(tau)
        s.p = float(position_index(d, p_cap))
        s.contribution = 0.0 if math.isinf(s.p) else s.L_cd_m2 ** 2 * s.omega_sr / s.p ** 2
        if s.omega_sr < omega_min:
            s.flags.append(f"small source: omega {s.omega_sr:.2e} sr < {omega_min:g}")
        if s.omega_sr > omega_max:
            s.flags.append(f"large source: omega {s.omega_sr:.3f} sr > {omega_max:g}")
        if s.p >= p_cap:
            s.flags.append(f"position index capped at {p_cap:g}")
        if math.isinf(s.p):
            s.flags.append("behind the line of sight: not counted")


def ugr(sources, L_b: float) -> float | None:
    if L_b <= 0:
        raise ValueError("background luminance must be positive")
    total = sum(s.contribution for s in sources)
    if total <= 0:
        return None
    return 8.0 * math.log10(0.25 / L_b * total)
