"""False-colour luminance map of one region, with the detected sources outlined
and numbered, windows hatched out and clipped pixels marked."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np


def _block_max(a: np.ndarray, n: int) -> np.ndarray:
    if n <= 1:
        return a
    h, w = a.shape[0] // n * n, a.shape[1] // n * n
    return a[:h, :w].reshape(h // n, n, w // n, n).max(axis=(1, 3))


def render_heatmap(path: str | Path, L: np.ndarray, labels: np.ndarray, sources,
                   exclude: np.ndarray, clipped: np.ndarray, title: str,
                   subtitle: str = "", units: str = "cd/m²", max_width: int = 1500) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import ListedColormap, LogNorm

    n = max(1, math.ceil(L.shape[1] / max_width))
    Ld = _block_max(L, n)                      # keep tiny bright LEDs visible
    lab = labels[::n, ::n][: Ld.shape[0], : Ld.shape[1]]
    exc = exclude[::n, ::n][: Ld.shape[0], : Ld.shape[1]]
    clp = _block_max(clipped.astype(np.uint8), n).astype(bool)

    pos = Ld[Ld > 0]
    vmin = max(float(np.percentile(pos, 1)) if pos.size else 0.1, 1e-3)
    vmax = max(float(Ld.max()), vmin * 10)

    h, w = Ld.shape
    n_lines = min(len(sources), 8) + (1 if len(sources) > 8 else 0) + (1 if sources else 0)
    fig_w, img_w = 11.0, 9.3                        # inches
    img_h = img_w * h / w
    top, gap, table = 0.45, 0.45, 0.17 * n_lines + 0.25
    fig_h = top + img_h + gap + table
    fig = plt.figure(figsize=(fig_w, fig_h), dpi=130)
    ax = fig.add_axes([0.45 / fig_w, (gap + table) / fig_h, img_w / fig_w, img_h / fig_h])
    im = ax.imshow(np.maximum(Ld, vmin), cmap="turbo", norm=LogNorm(vmin=vmin, vmax=vmax),
                   interpolation="nearest")
    if exc.any():
        ax.imshow(np.where(exc, 1.0, np.nan), cmap=ListedColormap(["#808080"]), alpha=0.55,
                  interpolation="nearest")
        ax.contourf(exc.astype(float), levels=[0.5, 1.5], colors="none", hatches=["///"])
    if clp.any():
        ax.imshow(np.where(clp, 1.0, np.nan), cmap=ListedColormap(["#ff00ff"]),
                  interpolation="nearest")
    if (lab > 0).any():
        ax.contour(lab > 0, levels=[0.5], colors="white", linewidths=0.9)
    for s in sources:
        x, y = s.centroid_px[0] / n, s.centroid_px[1] / n
        ax.annotate(str(s.id), (x, y), xytext=(6, -6), textcoords="offset points",
                    color="white", fontsize=9, fontweight="bold",
                    bbox=dict(boxstyle="round,pad=0.15", fc="black", ec="none", alpha=0.6))
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_title(title, fontsize=12, loc="left")
    if subtitle:
        ax.text(0, -0.02, subtitle, transform=ax.transAxes, fontsize=8.5, va="top")

    cax = fig.add_axes([(0.45 + img_w + 0.2) / fig_w, ax.get_position().y0, 0.22 / fig_w,
                        ax.get_position().height])
    cb = fig.colorbar(im, cax=cax)
    cb.set_label(f"Luminance [{units}]")

    lines = []
    for s in sorted(sources, key=lambda s: -s.contribution)[:8]:
        flag = "  *" if s.flags else ""
        lines.append(f"{s.id:>2}  {s.name:<14.14} L {s.L_cd_m2:>10,.0f}  "
                     f"ω {s.omega_sr:9.2e} sr  p {s.p:5.2f}  L²ω/p² {s.contribution:9.3g}{flag}")
    if len(sources) > 8:
        lines.append(f"    ... {len(sources) - 8} more in sources.csv")
    if lines:
        lines.append("* flagged - see sources.csv")
        fig.text(0.45 / fig_w, 0.1 / fig_h, "\n".join(lines), family="monospace", fontsize=8,
                 va="bottom")

    path = Path(path)
    fig.savefig(path)
    plt.close(fig)
    return path
