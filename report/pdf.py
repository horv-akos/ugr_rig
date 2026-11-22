"""The study report as a PDF: what someone who was not in the cab needs to read the result.

matplotlib's PdfPages (decided 2026-11-22: no new dependency on the Pi). Each page is a
figure, so the layout is fixed and the tables are short on purpose: the workbook carries the
full source lists. A4 portrait, light background: this one is printed, not read in a dark cab.

    1  the study: what was measured, the headline numbers, and every flag
    2  the forward gaze: coverage map and the sources that make the number (if rated)
    3+ one page per position: its source map with the UGR stamp, luminance map, top sources
    -  provenance: software, camera profile, k and its source, lens, thresholds, masks
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

A4 = (8.27, 11.69)
INK, MUTED, RULE = "#1b1f24", "#5f6b76", "#c9d1d9"
BAD, WARN, OK = "#b42318", "#9a6700", "#1a7f37"


def _ugr(v: float | None) -> str:
    return "—" if v is None else f"{v:.1f}"


def _colour(v: float | None) -> str:
    if v is None:
        return INK
    return BAD if v > 22 else WARN if v > 19 else OK


def _page(pdf, title: str, sub: str = ""):
    import matplotlib.pyplot as plt

    fig = plt.figure(figsize=A4, facecolor="white")
    fig.text(0.07, 0.955, title, fontsize=15, weight="bold", color=INK)
    if sub:
        fig.text(0.07, 0.935, sub, fontsize=8.5, color=MUTED)
    fig.add_artist(plt.Line2D([0.07, 0.93], [0.925, 0.925], color=RULE, lw=0.8))
    return fig


def _lines(fig, x: float, y: float, rows: list[tuple[str, str]], size: float = 9,
           key_w: float = 0.3, step: float = 0.019) -> float:
    for k, v in rows:
        fig.text(x, y, k, fontsize=size, color=MUTED)
        fig.text(x + key_w, y, v, fontsize=size, color=INK)
        y -= step
    return y


def _paragraphs(fig, x: float, y: float, items: list[str], width: int = 105, size: float = 8,
                bullet: str = "•", bottom: float = 0.05) -> float:
    for it in items:
        wrapped = textwrap.wrap(it, width) or [""]
        if y - 0.0145 * len(wrapped) < bottom:
            fig.text(x, y, f"… and {len(items) - items.index(it)} more (see the workbook)",
                     fontsize=size, color=MUTED)
            return y - 0.0145
        for i, line in enumerate(wrapped):
            fig.text(x, y, (bullet + " " if i == 0 else "   ") + line, fontsize=size, color=INK)
            y -= 0.0145
    return y


def _table(fig, rect: tuple[float, float, float, float], cols: list[str],
           rows: list[list[str]], size: float = 7.5) -> None:
    ax = fig.add_axes(rect)
    ax.axis("off")
    if not rows:
        ax.text(0, 1, "none", fontsize=size, color=MUTED, va="top")
        return
    t = ax.table(cellText=rows, colLabels=cols, loc="upper left", cellLoc="left",
                 colLoc="left", edges="horizontal")
    t.auto_set_font_size(False)
    t.set_fontsize(size)
    t.scale(1, 1.25)
    for (r, _), cell in t.get_celld().items():
        cell.set_edgecolor(RULE)
        if r == 0:
            cell.set_text_props(color=MUTED, weight="bold")


def _image(fig, path: Path, rect: tuple[float, float, float, float]) -> bool:
    if not path.is_file():
        return False
    import matplotlib.image as mpimg

    ax = fig.add_axes(rect)
    ax.imshow(mpimg.imread(str(path)))
    ax.axis("off")
    return True


def write(study_dir: str | Path, study: dict[str, Any], path: str | Path | None = None) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    d = Path(study_dir)
    out = Path(path) if path else d / "report.pdf"
    tmp = out.with_suffix(".pdf.tmp")
    prof, cam = study.get("profile") or {}, study.get("camera_profile") or {}
    sm, fw = study.get("summary") or {}, study.get("forward")
    sweep = (study.get("sweep") or {}).get("result") or {}
    drift, proc = study.get("drift"), study.get("processing") or {}
    title = f"{prof.get('name', '')}{' · ' + prof['vehicle'] if prof.get('vehicle') else ''}"

    with PdfPages(tmp, metadata={"Title": f"UGR study {study['id']}",
                                 "Subject": "Discomfort glare (CIE 117 UGR) in a truck cab"}) as pdf:
        # -- 1: the study ------------------------------------------------------------ #
        fig = _page(pdf, f"Glare study {study['id']}", title)
        heads = [("Forward gaze UGR", fw.get("ugr") if fw else None,
                  ("≥ " if fw and fw.get("lower_bound") and fw.get("ugr") is not None else "")
                  + _ugr(fw.get("ugr") if fw else None),
                  f"{round(100 * fw['coverage'])} % covered" if fw else "not rated"),
                 ("Highest UGR, camera axis", sm.get("ugr_max"), _ugr(sm.get("ugr_max")),
                  sm.get("ugr_max_position") or ""),
                 ("Positions over 19 / 22", None,
                  f"{sm.get('n_over_19', 0)} / {sm.get('n_over_22', 0)}",
                  f"of {sm.get('n_rated', 0)} rated")]
        for i, (k, v, text, sub) in enumerate(heads):
            x = 0.07 + i * 0.29
            fig.text(x, 0.885, k, fontsize=8.5, color=MUTED)
            fig.text(x, 0.845, text, fontsize=24, weight="bold", color=_colour(v))
            fig.text(x, 0.825, sub, fontsize=8, color=MUTED)
        y = _lines(fig, 0.07, 0.78, [
            ("Lighting", study.get("lighting", "")),
            ("Operator", study.get("operator") or "—"),
            ("Started / finished (UTC)", f"{study.get('started_at') or '—'} / "
                                         f"{study.get('finished_at') or '—'}"),
            ("Status", study.get("status", "")),
            ("Background L_b", f"{sweep['L_b']:.2f} cd/m² from the hemisphere sweep" if sweep
             else "each frame's own (provisional)"),
            ("Zero / drift", (f"optical; drift {drift['offset_deg']:.3f}° (limit "
                              f"{drift['limit_deg']}°)" if drift else
                              "optical, drift not measured" if study.get("zero") else
                              "set by hand, no drift figure")),
            ("Repeatability (check)", "—" if sm.get("check_delta_ugr") is None
             else f"{sm['check_delta_ugr']:+.2f} UGR at the first position shot again"),
            ("Camera profile", f"{cam.get('name', '—')}, k {cam.get('k') or proc.get('k')}"
                               f" ({cam.get('k_source') or 'simulator'})"),
        ])
        if study.get("notes"):
            y = _paragraphs(fig, 0.07, y - 0.01, [f"Notes: {study['notes']}"], bullet="")
        fig.text(0.07, y - 0.02, f"Flags ({len(study.get('flags', []))})", fontsize=10,
                 weight="bold", color=INK)
        _paragraphs(fig, 0.07, y - 0.045, study.get("flags") or ["none"])
        foot = textwrap.fill("UGR per CIE 117:1995. The forward gaze is the headline: every "
                             "source from every position, counted once, against the driver's "
                             "view of the road. Never average UGR values: the scale is "
                             "logarithmic.", 130)
        fig.text(0.07, 0.03, foot, fontsize=6.5, color=MUTED, va="bottom")
        pdf.savefig(fig)
        plt.close(fig)

        # -- 2: forward gaze ------------------------------------------------------- #
        if fw:
            fig = _page(pdf, f"Forward gaze: UGR {'≥ ' if fw.get('lower_bound') else ''}"
                             f"{_ugr(fw.get('ugr'))} ({fw.get('category', '')})",
                        f"gaze {fw.get('gaze')} · L_b {fw['L_b']:.2f} cd/m² ({fw.get('lb_method')})"
                        f" · coverage {100 * fw['coverage']:.0f} % (target "
                        f"{100 * fw['coverage_min']:.0f} %) · {fw['n_sources']} source(s), "
                        f"{fw.get('n_observations', 0)} observation(s)")
            _image(fig, d / "coverage.png", (0.05, 0.47, 0.9, 0.44))
            rows = [[s["name"][:34], s["path"], f"{s['L_cd_m2']:,.0f}", f"{s['omega_sr']:.2e}",
                     f"{s['p']:.2f}", f"{100 * s['share']:.0f} %", str(s["seen_from"])]
                    for s in fw.get("sources", [])[:18]]
            _table(fig, (0.07, 0.06, 0.86, 0.38),
                   ["Source", "Path", "L cd/m²", "ω sr", "p", "Share", "Views"], rows)
            pdf.savefig(fig)
            plt.close(fig)

        # -- 3+: positions -------------------------------------------------------------- #
        for p in study.get("positions", []) + ([study["check"]] if study.get("check") else []):
            r = p.get("result")
            if not r:
                fig = _page(pdf, p["name"], p.get("error") or p.get("state", ""))
                pdf.savefig(fig)
                plt.close(fig)
                continue
            fig = _page(pdf, f"{p['name']}: UGR {_ugr(r.get('ugr'))} ({r.get('category', '')})",
                        f"pan {p['pan_deg']:.2f}°, tilt {p['tilt_deg']:.2f}° · camera axis · "
                        f"L_b {r['L_b']:.2f} cd/m² ({r.get('lb_method')}) · "
                        f"{r.get('n_sources', 0)} source(s)"
                        + (f" · aim {p['aim']['total_deg']:.2f}° off" if p.get("aim") else ""))
            outs = r.get("outputs", {})
            _image(fig, d / outs.get("sources_map", "-"), (0.05, 0.52, 0.9, 0.39))
            _image(fig, d / outs.get("luminance_map", "-"), (0.05, 0.27, 0.43, 0.24))
            _image(fig, d / outs.get("contribution_map", "-"), (0.52, 0.27, 0.43, 0.24))
            rows = [[s["name"][:20], s.get("path", "direct"), f"{s['L_cd_m2']:,.0f}",
                     f"{s['omega_sr']:.2e}", f"{s['p']:.2f}", f"{100 * s['share']:.0f} %",
                     "" if s.get("delta_ugr") is None else f"−{s['delta_ugr']:.1f}"]
                    for s in r.get("sources", [])[:7]]
            _table(fig, (0.07, 0.1, 0.86, 0.16),
                   ["Source", "Path", "L cd/m²", "ω sr", "p", "Share", "UGR without"], rows)
            _paragraphs(fig, 0.07, 0.085, r.get("flags", [])[:4], size=7, bottom=0.01)
            pdf.savefig(fig)
            plt.close(fig)

        # -- provenance --------------------------------------------------------------- #
        fig = _page(pdf, "Provenance", "everything that can change a number")
        sw = study.get("software") or {}
        rows = [("Software", f"{(sw.get('commit') or 'unknown')[:12]}"
                             f"{' (uncommitted changes)' if sw.get('dirty') else ''}"),
                ("Position profile", f"{prof.get('name')} · hash {str(prof.get('content_hash'))[:12]}"),
                ("Camera profile", f"{cam.get('name')} · hash {str(cam.get('content_hash'))[:12]}"),
                ("k", f"{proc.get('k')} ({cam.get('k_source') or 'simulator'}"
                      f"{': ' + cam['k_note'] if cam.get('k_note') else ''})"),
                ("Lens", f"{cam.get('lens_name') or '—'}, {cam.get('focal_mm')} mm"),
                ("Exposure", f"f/{cam.get('aperture')}, ISO {cam.get('iso')}, "
                             f"{len(cam.get('shutter') or [])} frames"),
                ("Lighting", study.get("lighting", ""))]
        y = _lines(fig, 0.07, 0.89, rows, key_w=0.22)
        fig.text(0.07, y - 0.01, "Analysis overrides", fontsize=10, weight="bold", color=INK)
        ov = proc.get("overrides") or {}
        _paragraphs(fig, 0.07, y - 0.035, [f"{k} = {json.dumps(v)}" for k, v in sorted(ov.items())]
                    or ["none"], size=7.5)
        pdf.savefig(fig)
        plt.close(fig)
    tmp.replace(out)
    return out
