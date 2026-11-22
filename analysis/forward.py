"""The study's headline: one UGR for the driver looking at the road (REQUIREMENTS §3.6).

Each position rates its own frame as if the driver looked along the camera axis. The forward
gaze rates the cab as the driver actually sees it while driving: every source from every
position, placed in mount coordinates, counted once, against one fixed line of sight with the
session L_b.

Counted once. Frames overlap, so one source is often seen from two or three positions. Two
detections are the same source when their angular footprints overlap in mount coordinates
(each footprint a cap of the source's own solid angle, plus `match_tolerance_deg` for the
head's repeatability); two sources in one frame are never merged, detection already decided
that. Of each group the view that lies wholly inside its frame and nearest the frame centre is
kept (least vignetting and distortion, omega not truncated). The others are recorded against
it: two views of one source should agree in luminance, which is a free consistency check.

Coverage. Sources outside every frame are not counted, so the number is only as complete as the
positions. The forward hemisphere around the gaze is sampled, each direction weighted by its
solid angle and by 1/p^2, how much a source there would count. The covered fraction of that
weight is the glare-weighted coverage: a gap just below the line of sight matters, a gap
overhead at p ~ 10 hardly does. Below `coverage_min` the forward-gaze UGR is a lower bound and
flagged. The same weighting proposes a tiling of the forward field for the position profile.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from photometry.masks import angles, position_index, rotation


@dataclass
class Observation:
    position: str
    name: str
    path: str
    reflects: str | None
    source_type: str | None
    L_cd_m2: float
    omega_sr: float
    direction: tuple[float, float, float]   # mount (cabin) frame
    pan_deg: float                          # the source's own direction, mount angles
    tilt_deg: float
    off_centre_deg: float                   # from its frame's optical axis
    cut: bool
    flags: list[str]

    @property
    def radius_deg(self) -> float:
        return math.degrees(math.sqrt(max(self.omega_sr, 0.0) / math.pi))


@dataclass
class ForwardSource:
    name: str                    # "<position>: <source>"
    kept: Observation
    others: list[dict[str, Any]] = field(default_factory=list)
    sigma_deg: float = float("nan")
    tau_deg: float = float("nan")
    p: float = float("nan")
    contribution: float = 0.0
    share: float = float("nan")
    flags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        k = self.kept
        return {"name": self.name, "position": k.position, "source": k.name, "path": k.path,
                "reflects": k.reflects, "type": k.source_type, "L_cd_m2": k.L_cd_m2,
                "omega_sr": k.omega_sr, "pan_deg": round(k.pan_deg, 3),
                "tilt_deg": round(k.tilt_deg, 3), "sigma_deg": self.sigma_deg,
                "tau_deg": self.tau_deg, "p": self.p, "contribution": self.contribution,
                "share": self.share, "cut": k.cut, "seen_from": len(self.others) + 1,
                "others": self.others, "flags": self.flags}


def mount_angles(d: np.ndarray) -> tuple[float, float]:
    return (math.degrees(math.atan2(float(d[0]), float(d[2]))),
            math.degrees(math.asin(max(-1.0, min(1.0, float(d[1]))))))


def observations(position: str, pan: float, tilt: float, sources: list[dict[str, Any]]
                 ) -> list[Observation]:
    """A position's sources (result.json dicts) in mount coordinates."""
    R = rotation(pan, tilt)
    out = []
    for s in sources:
        dc = np.asarray(s["direction_cam"], float)
        d = R @ dc
        p, t = mount_angles(d)
        out.append(Observation(
            position=position, name=s["name"], path=s.get("path", "direct"),
            reflects=s.get("reflects"), source_type=s.get("source_type"),
            L_cd_m2=float(s["L_cd_m2"]), omega_sr=float(s["omega_sr"]),
            direction=tuple(float(x) for x in d), pan_deg=p, tilt_deg=t,
            off_centre_deg=math.degrees(math.acos(max(-1.0, min(1.0, float(dc[2]))))),
            cut=bool(s.get("cut", False)),
            flags=[f for f in s.get("flags", []) if not f.startswith(("small source",
                                                                       "large source",
                                                                       "position index",
                                                                       "behind"))]))
    return out


def deduplicate(obs: list[Observation], tolerance_deg: float, mismatch_warn: float
                ) -> list[ForwardSource]:
    """One ForwardSource per physical source (see the module docstring)."""
    n = len(obs)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    dirs = np.array([o.direction for o in obs], float).reshape(n, 3)
    for i in range(n):
        for j in range(i + 1, n):
            if obs[i].position == obs[j].position:
                continue
            ang = math.degrees(math.acos(max(-1.0, min(1.0, float(dirs[i] @ dirs[j])))))
            if ang <= obs[i].radius_deg + obs[j].radius_deg + tolerance_deg:
                parent[find(i)] = find(j)
    groups: dict[int, list[Observation]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(obs[i])

    out = []
    for members in groups.values():
        members.sort(key=lambda o: (o.cut, o.off_centre_deg))
        kept = members[0]
        fs = ForwardSource(name=f"{kept.position}: {kept.name}", kept=kept,
                           flags=list(kept.flags))
        for o in members[1:]:
            ratio = o.L_cd_m2 / kept.L_cd_m2 if kept.L_cd_m2 > 0 else float("nan")
            fs.others.append({"position": o.position, "source": o.name, "cut": o.cut,
                              "L_ratio": round(ratio, 4),
                              "off_centre_deg": round(o.off_centre_deg, 2)})
            if not o.cut and not kept.cut and abs(ratio - 1) > mismatch_warn:
                fs.flags.append(f"seen from {o.position} at {100 * (ratio - 1):+.0f} % "
                                "luminance: the two views disagree")
        if kept.cut:
            fs.flags = [f for f in fs.flags if not f.startswith("cut by the frame edge")]
            fs.flags.append("cut by the frame edge in every view: omega is a lower bound")
        elif any(o.cut for o in members[1:]):
            fs.flags = [f for f in fs.flags if not f.startswith("cut by the frame edge")]
        out.append(fs)
    return out


def rate(sources: list[ForwardSource], gaze: tuple[float, float], L_b: float, omega_min: float,
         omega_max: float, p_cap: float) -> float | None:
    """Position index against the gaze, contributions, shares; returns the UGR."""
    if L_b <= 0:
        raise ValueError("background luminance must be positive")
    G = rotation(*gaze).T                 # mount -> view
    for s in sources:
        d = G @ np.asarray(s.kept.direction, float)
        sigma, tau = angles(d)
        s.sigma_deg, s.tau_deg = float(sigma), float(tau)
        s.p = float(position_index(d, p_cap))
        k = s.kept
        s.contribution = 0.0 if math.isinf(s.p) else k.L_cd_m2 ** 2 * k.omega_sr / s.p ** 2
        if k.omega_sr < omega_min:
            s.flags.append(f"small source: omega {k.omega_sr:.2e} sr < {omega_min:g}")
        if k.omega_sr > omega_max:
            s.flags.append(f"large source: omega {k.omega_sr:.3f} sr > {omega_max:g}")
        if math.isinf(s.p):
            s.flags.append("behind the forward gaze: not counted")
        elif s.p >= p_cap:
            s.flags.append(f"position index capped at {p_cap:g}")
    total = sum(s.contribution for s in sources)
    for s in sources:
        s.share = s.contribution / total if total > 0 else 0.0
    return 8.0 * math.log10(0.25 / L_b * total) if total > 0 else None


# --------------------------------------------------------------------------- #
# coverage and tiling
# --------------------------------------------------------------------------- #


@dataclass
class CoverageGrid:
    az: np.ndarray            # degrees from the gaze, right positive (cell centres)
    el: np.ndarray            # degrees from the gaze, up positive
    weight: np.ndarray        # (n_el, n_az) solid angle / p^2, 0 behind
    dirs: np.ndarray          # (n_el, n_az, 3) mount frame


def hemisphere(gaze: tuple[float, float], p_cap: float, bin_deg: float = 1.0) -> CoverageGrid:
    """The forward hemisphere around the gaze on an az/el grid, each cell weighted by what a
    source there would count: its solid angle over p^2."""
    az = np.arange(-90 + bin_deg / 2, 90, bin_deg)
    el = np.arange(-90 + bin_deg / 2, 90, bin_deg)
    A, E = np.meshgrid(np.radians(az), np.radians(el))
    v = np.stack([np.sin(A) * np.cos(E), np.sin(E), np.cos(A) * np.cos(E)], axis=-1)
    p = position_index(v, p_cap)
    cell = np.radians(bin_deg) ** 2 * np.cos(E)
    with np.errstate(divide="ignore"):
        weight = np.where(np.isfinite(p), cell / p ** 2, 0.0)
    return CoverageGrid(az=az, el=el, weight=weight, dirs=v @ rotation(*gaze).T)


def covered(grid: CoverageGrid, frames: list[tuple[float, float]], fov_deg: tuple[float, float]
            ) -> np.ndarray:
    """Cells seen by at least one frame (pan, tilt) of this field of view."""
    tx, ty = math.tan(math.radians(fov_deg[0] / 2)), math.tan(math.radians(fov_deg[1] / 2))
    seen = np.zeros(grid.weight.shape, bool)
    for pan, tilt in frames:
        c = grid.dirs @ rotation(pan, tilt)          # mount -> camera: d @ R == R.T d
        z = c[..., 2]
        with np.errstate(divide="ignore", invalid="ignore"):
            seen |= (z > 0) & (np.abs(c[..., 0]) <= tx * z) & (np.abs(c[..., 1]) <= ty * z)
    return seen


def coverage(grid: CoverageGrid, seen: np.ndarray) -> float:
    total = float(grid.weight.sum())
    return float(grid.weight[seen].sum()) / total if total > 0 else 0.0


def propose_tiling(gaze: tuple[float, float], fov_deg: tuple[float, float],
                   limits: dict[str, list[float]], p_cap: float, target: float,
                   overlap: float = 0.1, existing: list[tuple[float, float]] | None = None,
                   bin_deg: float = 2.0, max_frames: int = 12) -> dict[str, Any]:
    """Frames that bring the glare-weighted coverage to `target`, chosen greedily from a grid
    around the gaze (steps of the field of view less `overlap`), each pick the one that adds
    the most weight, inside the soft limits. `existing` frames count as already there."""
    grid = hemisphere(gaze, p_cap, bin_deg)
    seen = covered(grid, list(existing or []), fov_deg)
    step_p, step_t = fov_deg[0] * (1 - overlap), fov_deg[1] * (1 - overlap)
    (pmin, pmax), (tmin, tmax) = limits["pan"], limits["tilt"]
    cands = []
    for i in range(-3, 4):
        for j in range(-2, 3):
            p = round(min(pmax, max(pmin, gaze[0] + i * step_p)), 2)
            t = round(min(tmax, max(tmin, gaze[1] + j * step_t)), 2)
            if (p, t) not in cands:
                cands.append((p, t))
    masks = {c: covered(grid, [c], fov_deg) for c in cands}
    picks: list[tuple[float, float]] = []
    cov = coverage(grid, seen)
    while cov < target and len(picks) < max_frames:
        gains = {c: float(grid.weight[m & ~seen].sum()) for c, m in masks.items() if c not in picks}
        if not gains:
            break
        best = max(gains, key=gains.get)
        if gains[best] <= 0:
            break
        picks.append(best)
        seen |= masks[best]
        cov = coverage(grid, seen)
    return {"frames": [list(c) for c in picks], "coverage": cov, "target": target,
            "overlap": overlap, "reached": cov >= target}


def coverage_png(gaze: tuple[float, float], fov_deg: tuple[float, float],
                 frames: list[tuple[str, float, float]], p_cap: float,
                 sources: list[dict[str, Any]] | None = None,
                 proposed: list[tuple[float, float]] | None = None,
                 title: str = "") -> bytes:
    """A map of the forward hemisphere around the gaze: glare weight (how much a source there
    would count), the frames' outlines, what they leave uncovered, and the sources."""
    import io

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from report.images import BG, FG, MUTED

    grid = hemisphere(gaze, p_cap, 1.0)
    seen = covered(grid, [(p, t) for _, p, t in frames], fov_deg)
    cov = coverage(grid, seen)
    w = np.where(grid.weight > 0, grid.weight / grid.weight.max(), np.nan)
    fig, ax = plt.subplots(figsize=(8.4, 5.2), dpi=100, facecolor=BG)
    ax.set_facecolor(BG)
    ext = (-90, 90, -90, 90)
    rgb = plt.get_cmap("magma")(np.clip((np.log10(np.nan_to_num(w, nan=1e-9)) + 3) / 3, 0, 1))
    rgb[..., :3] *= np.where(seen, 1.0, 0.28)[..., None]          # not covered: dimmed
    rgb[grid.weight <= 0] = (0, 0, 0, 0)
    ax.imshow(rgb, origin="lower", extent=ext, aspect="auto")

    def outline(pan: float, tilt: float, n: int = 24) -> np.ndarray:
        tx = math.tan(math.radians(fov_deg[0] / 2))
        ty = math.tan(math.radians(fov_deg[1] / 2))
        s = np.linspace(-1, 1, n)
        edge = np.concatenate([np.stack([s * tx, -ty + 0 * s], 1), np.stack([tx + 0 * s, s * ty], 1),
                               np.stack([-s * tx, ty + 0 * s], 1), np.stack([-tx + 0 * s, -s * ty], 1)])
        d = np.concatenate([edge, np.ones((len(edge), 1))], 1)
        d = d / np.linalg.norm(d, axis=1, keepdims=True)
        v = d @ (rotation(*gaze).T @ rotation(pan, tilt)).T
        return np.degrees(np.stack([np.arctan2(v[:, 0], v[:, 2]), np.arcsin(np.clip(v[:, 1], -1, 1))], 1))

    for name, p, t in frames:
        xy = outline(p, t)
        ax.plot(xy[:, 0], xy[:, 1], color=FG, lw=1)
        c = xy.mean(0)
        ax.text(c[0], c[1], name, color=FG, fontsize=7, ha="center", va="center")
    for p, t in proposed or []:
        xy = outline(p, t)
        ax.plot(xy[:, 0], xy[:, 1], color="#ffc440", lw=1, ls="--")
    for s in sources or []:
        v = rotation(*gaze).T @ rotation(s["pan_deg"], s["tilt_deg"])[:, 2]
        a, e = math.degrees(math.atan2(v[0], v[2])), math.degrees(math.asin(max(-1, min(1, v[1]))))
        ax.plot(a, e, marker="o" if s.get("path") != "reflection" else "D", ms=4,
                color="#f2554b" if s.get("path") != "reflection" else "#5ad1ff",
                mec=FG, mew=0.5)
    ax.axhline(0, color=MUTED, lw=0.5)
    ax.axvline(0, color=MUTED, lw=0.5)
    ax.plot(0, 0, marker="+", color=FG, ms=10)
    ax.set_xlim(-90, 90)
    ax.set_ylim(-90, 90)
    ax.set_xlabel("degrees right of the gaze", color=MUTED, fontsize=8)
    ax.set_ylabel("degrees above the gaze", color=MUTED, fontsize=8)
    ax.tick_params(colors=MUTED, labelsize=8)
    for sp in ax.spines.values():
        sp.set_color(MUTED)
    ax.set_title((title + " · " if title else "") + f"glare-weighted coverage {100 * cov:.0f} %"
                 "\nbright: how much a source there would count (1/p², log) · "
                 "dimmed: no frame sees it", color=FG, fontsize=9)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def summary(sources: list[ForwardSource], ugr: float | None, L_b: float, cov: float,
            coverage_min: float) -> dict[str, Any]:
    return {"ugr": ugr, "L_b": L_b, "coverage": cov, "coverage_min": coverage_min,
            "lower_bound": cov < coverage_min,
            "n_sources": len(sources),
            "n_reflections": sum(s.kept.path == "reflection" for s in sources),
            "reflection_share": float(sum(s.share for s in sources if s.kept.path == "reflection")),
            "sources": [s.as_dict() for s in sorted(sources, key=lambda x: -x.contribution)]}


__all__ = ["Observation", "ForwardSource", "observations", "deduplicate", "rate", "hemisphere",
           "covered", "coverage", "propose_tiling", "coverage_png", "summary", "asdict"]
