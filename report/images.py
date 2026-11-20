"""The pictures of one position (REQUIREMENTS §5.5), each stamped with its UGR in a corner.

    photo.jpg            the scene: the JPEG the 2000D embeds in the best-exposed CR2, or, for
                         the simulator's frames, a tone-mapped render of the luminance map
    luminance.png        log false colour on a FIXED scale (config [report]), so the same colour
                         is the same luminance in every study
    contribution.png     where the UGR comes from: each source pixel's share of sum L^2 omega/p^2
    sources.png          the photo with a box per source: id, name, L, share; dashed if flagged
    masks.png            position-index rings (how much a source there would count) and the
                         exclusion, glazing and luminaire polygons

Dark on purpose, like the pages: these are looked at in a dark cab. The stamp carries a warning
mark when the result has flags, so a number never travels without its caveat.
"""

from __future__ import annotations

import io
import math
from pathlib import Path
from typing import Any

import numpy as np

from photometry.masks import CameraGeometry, position_index

BG = "#0b0d10"
FG = "#e4e7eb"
MUTED = "#8c96a1"
AMBER = "#ffc440"
CYAN = "#5ad1ff"
RED = "#f2554b"
WIDTH = 1200                      # display width of every image, px
P_LEVELS = (1.25, 1.5, 2.0, 3.0, 5.0, 8.0)


def _block(a: np.ndarray, n: int, how: str) -> np.ndarray:
    if n <= 1:
        return a
    h, w = a.shape[0] // n * n, a.shape[1] // n * n
    b = a[:h, :w].reshape(h // n, n, w // n, n)
    return getattr(b, how)(axis=(1, 3))


def tone_map(L: np.ndarray) -> np.ndarray:
    """A plausible photo from luminance alone: log, 1st..99.5th percentile, gamma."""
    lg = np.log10(np.maximum(L, 1e-3))
    lo, hi = np.percentile(lg, [1, 99.5])
    v = np.clip((lg - lo) / max(hi - lo, 1e-6), 0, 1) ** 0.8
    return (np.repeat(v[..., None], 3, axis=2) * 255).astype(np.uint8)


def photo_from_bracket(bracket_dir: Path, frames: list, L_median: float, k: float
                       ) -> np.ndarray | None:
    """The embedded JPEG of the frame that shows the scene's median near mid-grey. Cosmetic:
    nothing is measured from it. None for frames that carry no JPEG (the simulator's)."""
    cr2 = [f for f in frames if str(f.file).lower().endswith(".cr2")]
    if not cr2:
        return None
    # DN of the median ~ L k^-1 t ISO / N^2; aim at ~2000 DN, a mid-grey on a 14-bit raw
    best = min(cr2, key=lambda f: abs(math.log(max(
        L_median / k * f.t_effective_s * f.iso / f.aperture ** 2, 1e-9) / 2000.0)))
    try:
        import rawpy
        from PIL import Image

        with rawpy.imread(str(bracket_dir / best.file)) as raw:
            thumb = raw.extract_thumb()
        if thumb.format != rawpy.ThumbFormat.JPEG:
            return None
        return np.asarray(Image.open(io.BytesIO(thumb.data)).convert("RGB"))
    except Exception:  # noqa: BLE001 - a missing preview only costs the photo
        return None


def _resize(rgb: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    from PIL import Image

    h, w = shape
    if rgb.shape[:2] == (h, w):
        return rgb
    return np.asarray(Image.fromarray(rgb).resize((w, h), Image.LANCZOS))


class _Fig:
    """A borderless figure the size of the image, optionally with a strip for a colour bar."""

    def __init__(self, h: int, w: int, strip: int = 0) -> None:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        self.plt = plt
        self.h, self.w = h, w
        dpi = 100
        self.fig = plt.figure(figsize=((w + strip) / dpi, h / dpi), dpi=dpi, facecolor=BG)
        self.ax = self.fig.add_axes([0, 0, w / (w + strip), 1])
        self.ax.set_axis_off()
        self.ax.set_xlim(-0.5, w - 0.5)
        self.ax.set_ylim(h - 0.5, -0.5)
        self.cax = (self.fig.add_axes([(w + 12) / (w + strip), 0.06, 14 / (w + strip), 0.88])
                    if strip else None)
        self.fs = max(9.0, w / 95)

    def stamp(self, stamp: dict[str, Any]) -> None:
        u = stamp.get("ugr")
        head = f"UGR {u:.1f}" if u is not None else "UGR n/a"
        sub = " · ".join(x for x in (stamp.get("position"), stamp.get("study")) if x)
        nflags = stamp.get("n_flags", 0)
        warn = f"   ⚠ {nflags} flag{'s' if nflags != 1 else ''}" if nflags else ""
        self.ax.text(0.012, 0.985, f"{head}  {stamp.get('category', '')}",
                     transform=self.ax.transAxes, va="top", ha="left", color=FG,
                     fontsize=self.fs * 1.5, fontweight="bold",
                     bbox=dict(boxstyle="round,pad=0.35", fc="black", ec="none", alpha=0.65))
        if sub or warn:
            self.ax.text(0.012, 0.985 - self.fs * 3.4 / self.h, sub + warn,
                         transform=self.ax.transAxes, va="top", ha="left",
                         color=AMBER if warn else MUTED, fontsize=self.fs,
                         bbox=dict(boxstyle="round,pad=0.3", fc="black", ec="none", alpha=0.65))

    def save(self, path: Path, **kw) -> None:
        self.fig.savefig(path, facecolor=BG, **kw)
        self.plt.close(self.fig)


def render_all(out: Path, L: np.ndarray, labels: np.ndarray, sources: list, masks,
               exclude: np.ndarray, clipped: np.ndarray, geom: CameraGeometry,
               cam_to_view: np.ndarray, stamp: dict[str, Any], scale: tuple[float, float],
               photo: np.ndarray | None = None, p_cap: float = 16.0) -> dict[str, str]:
    from matplotlib import cm
    from matplotlib.colors import LogNorm
    from matplotlib.patches import Polygon, Rectangle

    H, W = L.shape
    n = max(1, math.ceil(W / WIDTH))
    Ld = _block(L, n, "max")                     # max keeps tiny bright LEDs visible
    h, w = Ld.shape
    base = _resize(photo, (h, w)) if photo is not None else tone_map(_block(L, n, "mean"))
    photo_src = "camera JPEG" if photo is not None else "rendered from luminance"
    outputs: dict[str, str] = {}

    def to_disp(poly):
        return [(x * w - 0.5, y * h - 0.5) for x, y in poly]

    # -- photo --------------------------------------------------------------- #
    f = _Fig(h, w)
    f.ax.imshow(base, interpolation="bilinear")
    f.ax.text(0.988, 0.015, photo_src, transform=f.ax.transAxes, ha="right", va="bottom",
              color=MUTED, fontsize=f.fs * 0.8)
    f.stamp(stamp)
    f.save(out / "photo.jpg", pil_kwargs={"quality": 88})
    outputs["photo"] = "photo.jpg"

    # -- luminance, fixed scale -------------------------------------------------- #
    lo, hi = scale
    f = _Fig(h, w, strip=90)
    im = f.ax.imshow(np.clip(Ld, lo, hi), cmap="turbo", norm=LogNorm(lo, hi),
                     interpolation="nearest")
    clp = _block(clipped.astype(np.uint8), n, "max").astype(bool)
    if clp.any():
        f.ax.imshow(np.where(clp, 1.0, np.nan), cmap=_flat("#ff00ff"), interpolation="nearest")
    cb = f.fig.colorbar(im, cax=f.cax)
    cb.set_label("cd/m²", color=FG, fontsize=f.fs)
    cb.ax.tick_params(colors=FG, labelsize=f.fs * 0.85)
    cb.outline.set_edgecolor(MUTED)
    f.stamp(stamp)
    f.save(out / "luminance.png")
    outputs["luminance_map"] = "luminance.png"

    # -- contribution ---------------------------------------------------------- #
    total = sum(s.contribution for s in sources)
    share = np.zeros((H, W), np.float64)
    if total > 0 and (labels > 0).any():
        vv, uu = np.nonzero(labels > 0)
        d = geom.pixel_directions(uu, vv) @ cam_to_view.T
        p = position_index(d, p_cap)
        c = L[vv, uu].astype(np.float64) ** 2 * geom.solid_angle[vv, uu] / p ** 2
        share[vv, uu] = np.where(np.isfinite(c), c, 0) / total
    sd = _block(share, n, "sum")
    f = _Fig(h, w, strip=110)
    f.ax.imshow((base * 0.3).astype(np.uint8), interpolation="bilinear")
    pos = sd[sd > 0]
    if pos.size:
        vmin = max(pos.min(), pos.max() * 1e-5)
        im = f.ax.imshow(np.where(sd > 0, np.maximum(sd, vmin), np.nan), cmap="inferno",
                         norm=LogNorm(vmin, pos.max()), interpolation="nearest")
        from matplotlib.ticker import FuncFormatter

        cb = f.fig.colorbar(im, cax=f.cax, format=FuncFormatter(_pct))
        cb.set_label("share of the glare per pixel", color=FG, fontsize=f.fs * 0.9)
        cb.ax.tick_params(colors=FG, labelsize=f.fs * 0.8)
        cb.outline.set_edgecolor(MUTED)
    for s in sources:
        if s.share and s.share >= 0.01:
            x, y = s.centroid_px[0] / n, s.centroid_px[1] / n
            right = x > 0.8 * w
            f.ax.text(x - 8 if right else x + 8, y - 8, f"{s.id}  {100 * s.share:.0f} %",
                      ha="right" if right else "left", color=FG, fontsize=f.fs * 0.9,
                      bbox=dict(fc="black", ec="none", alpha=0.6, pad=1.5))
    f.stamp(stamp)
    f.save(out / "contribution.png")
    outputs["contribution_map"] = "contribution.png"

    # -- sources ----------------------------------------------------------------- #
    f = _Fig(h, w)
    f.ax.imshow(base, interpolation="bilinear")
    for poly in masks.exclude:
        f.ax.add_patch(Polygon(to_disp(poly), closed=True, fc=(0.5, 0.5, 0.5, 0.35),
                               ec=MUTED, hatch="///", lw=1))
    for poly in masks.glazing:
        f.ax.add_patch(Polygon(to_disp(poly), closed=True, fill=False, ec=CYAN, lw=1.2,
                               ls=(0, (4, 3))))
    for s in sorted(sources, key=lambda s: -s.contribution):
        x0, y0, x1, y1 = (v / n for v in s.bbox_px)
        pad = 3
        f.ax.add_patch(Rectangle((x0 - pad, y0 - pad), x1 - x0 + 2 * pad, y1 - y0 + 2 * pad,
                                 fill=False, ec=AMBER, lw=2 if s.share >= 0.2 else 1.3,
                                 ls="--" if s.flags else "-"))
        txt = (f"{s.id} {s.name}  {s.L_cd_m2:,.0f} cd/m²  "
               f"{100 * s.share:.0f} %" + ("  *" if s.flags else ""))
        right = x0 > 0.55 * w        # near the right edge: hang the label leftwards
        f.ax.text(x1 + pad if right else x0 - pad, y1 + pad + 2, txt, va="top",
                  ha="right" if right else "left", color=AMBER, fontsize=f.fs * 0.85,
                  bbox=dict(fc="black", ec="none", alpha=0.65, pad=1.5))
    if not sources:
        f.ax.text(0.5, 0.06, "no source at or above the threshold", transform=f.ax.transAxes,
                  ha="center", color=MUTED, fontsize=f.fs)
    f.stamp(stamp)
    f.save(out / "sources.png")
    outputs["sources_map"] = "sources.png"

    # -- masks and position index -------------------------------------------- #
    f = _Fig(h, w)
    f.ax.imshow((base * 0.6).astype(np.uint8), interpolation="bilinear")
    step = max(1, h // 120)
    gy, gx = np.mgrid[0:h:step, 0:w:step]
    d = geom.pixel_directions((gx + 0.5) * n, (gy + 0.5) * n) @ cam_to_view.T
    p = position_index(d, p_cap)
    p = np.where(np.isfinite(p), p, p_cap)
    cs = f.ax.contour(gx, gy, p, levels=P_LEVELS, colors=[FG], linewidths=0.9, alpha=0.8)
    f.ax.clabel(cs, fmt=lambda v: f"p {v:g}", fontsize=f.fs * 0.75, colors=FG)
    for poly in masks.exclude:
        f.ax.add_patch(Polygon(to_disp(poly), closed=True, fc=(0.95, 0.33, 0.29, 0.25), ec=RED,
                               lw=1.5))
    for poly in masks.glazing:
        f.ax.add_patch(Polygon(to_disp(poly), closed=True, fc=(0.35, 0.82, 1.0, 0.18), ec=CYAN,
                               lw=1.5))
    for roi in masks.include:
        pts = to_disp(roi.polygon)
        f.ax.add_patch(Polygon(pts, closed=True, fc=(1.0, 0.77, 0.25, 0.18), ec=AMBER, lw=1.5))
        cx = sum(x for x, _ in pts) / len(pts)
        cy = sum(y for _, y in pts) / len(pts)
        f.ax.text(cx, cy, roi.name, ha="center", va="center", color=AMBER, fontsize=f.fs * 0.85,
                  bbox=dict(fc="black", ec="none", alpha=0.6, pad=1.5))
    f.ax.text(0.988, 0.015, "rings: Guth/Iwata position index p; a source counts 1/p²",
              transform=f.ax.transAxes, ha="right", va="bottom", color=MUTED,
              fontsize=f.fs * 0.8)
    f.stamp(stamp)
    f.save(out / "masks.png")
    outputs["masks_map"] = "masks.png"
    return outputs


def geometry_png(geom: CameraGeometry, p_cap: float = 16.0) -> bytes:
    """A lens's geometry masks side by side: solid angle per pixel, and the position
    index along the camera axis (how much a source at each point would count)."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    step = max(1, geom.width // 300)
    gy, gx = np.mgrid[0:geom.height:step, 0:geom.width:step]
    omega = geom.solid_angle[::step, ::step]
    p = position_index(geom.pixel_directions(gx, gy), p_cap)
    fig, axes = plt.subplots(1, 2, figsize=(11, 3.6), dpi=100, facecolor=BG)
    for ax, data, title, cmap in ((axes[0], omega * 1e6, "solid angle per pixel (µsr)", "viridis"),
                                  (axes[1], p, "position index p (camera axis)", "magma_r")):
        im = ax.imshow(data, cmap=cmap, extent=(0, geom.width, geom.height, 0))
        ax.set_title(title, color=FG, fontsize=10)
        ax.tick_params(colors=MUTED, labelsize=8)
        for sp in ax.spines.values():
            sp.set_color(MUTED)
        cb = fig.colorbar(im, ax=ax, fraction=0.04)
        cb.ax.tick_params(colors=FG, labelsize=8)
    cs = axes[1].contour(gx, gy, p, levels=P_LEVELS, colors=[FG], linewidths=0.8,
                         extent=None)
    axes[1].clabel(cs, fmt=lambda v: f"{v:g}", fontsize=7, colors=FG)
    fh, fv = geom.fov_deg()
    fig.suptitle(f"{geom.focal_mm:g} mm, k1 {geom.k1:g}: {fh:.1f} x {fv:.1f} deg, "
                 f"{geom.solid_angle.sum():.3f} sr per frame", color=FG, fontsize=10)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def _flat(color: str):
    from matplotlib.colors import ListedColormap

    return ListedColormap([color])


def _pct(x, _pos=None) -> str:
    return f"{100 * x:.2g} %"
