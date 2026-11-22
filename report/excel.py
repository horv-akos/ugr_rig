"""The study as one Excel workbook (XlsxWriter, BSD): what gets mailed and compared.

Built from the study's own folder only (study.json and each position's result.json), so it can
be rebuilt at any time and always matches the record.

    Summary            the headline numbers, conditions, flags
    Positions          one row per position: UGR, L_b, metrics, threshold sensitivity
    Sources            one row per source: L, omega, p, share, UGR change without it, ...
    Movement profile   the position profile as recorded: zero reference, gaze, angles
    Camera profile     body, lens, exposure, geometry and the analysis config that applied
    Calibration & QA   k, how it was obtained, check position, sweep, merge quality
    Provenance         frame hashes, software commit, config snapshot per position

UGR values are never averaged here either: the scale is logarithmic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _results(study_dir: Path, study: dict[str, Any]) -> list[tuple[dict, dict | None]]:
    out = []
    for p in study.get("positions", []) + ([study["check"]] if study.get("check") else []):
        r = p.get("result") or {}
        f = study_dir / r["dir"] / "result.json" if r.get("dir") else None
        out.append((p, json.loads(f.read_text()) if f and f.is_file() else None))
    return out


def write(study_dir: str | Path, study: dict[str, Any], path: str | Path | None = None) -> Path:
    import xlsxwriter

    study_dir = Path(study_dir)
    path = Path(path) if path else study_dir / "report.xlsx"
    rows = _results(study_dir, study)
    wb = xlsxwriter.Workbook(str(path), {"nan_inf_to_errors": True})
    head = wb.add_format({"bold": True, "bg_color": "#1c2128", "font_color": "#e4e7eb",
                          "border": 1, "text_wrap": True, "valign": "top"})
    key = wb.add_format({"bold": True})
    wrap = wb.add_format({"text_wrap": True, "valign": "top"})
    n1, n2, n3 = (wb.add_format({"num_format": f}) for f in ("0.0", "0.00", "0.000"))
    sci = wb.add_format({"num_format": "0.00E+00"})
    pct = wb.add_format({"num_format": "0.0%"})
    bad = wb.add_format({"font_color": "#c0392b", "bold": True})
    warn = wb.add_format({"font_color": "#b9770e", "bold": True})

    def table(ws, columns: list[tuple[str, int, Any]], data: list[list[Any]], row0: int = 0):
        for c, (title, width, fmt) in enumerate(columns):
            ws.write(row0, c, title, head)
            ws.set_column(c, c, width, fmt)
        for r, values in enumerate(data, start=row0 + 1):
            for c, v in enumerate(values):
                if v is None or (isinstance(v, float) and v != v):
                    ws.write_blank(r, c, None)
                elif isinstance(v, (list, dict)):
                    ws.write(r, c, json.dumps(v))
                else:
                    ws.write(r, c, v)
        ws.freeze_panes(row0 + 1, 1)
        if data:
            ws.autofilter(row0, 0, row0 + len(data), len(columns) - 1)
        return row0 + len(data) + 1

    def ugr_colours(ws, col: int, first: int, last: int) -> None:
        if last < first:
            return
        ws.conditional_format(first, col, last, col,
                              {"type": "cell", "criteria": ">", "value": 22, "format": bad})
        ws.conditional_format(first, col, last, col,
                              {"type": "cell", "criteria": "between", "minimum": 19.0001,
                               "maximum": 22, "format": warn})

    sm = study.get("summary") or {}
    sweep = (study.get("sweep") or {}).get("result") or {}
    proc = study.get("processing") or {}

    # -- Summary -------------------------------------------------------------- #
    ws = wb.add_worksheet("Summary")
    ws.set_column(0, 0, 30, key)
    ws.set_column(1, 1, 70, wrap)
    items = [
        ("Study", study["id"]), ("Status", study.get("status")),
        ("Profile", study["profile"]["name"]), ("Vehicle / configuration", study["profile"].get("vehicle")),
        ("Lighting", study.get("lighting")), ("Operator", study.get("operator")),
        ("Notes", study.get("notes")), ("Started (UTC)", study.get("started_at")),
        ("Finished (UTC)", study.get("finished_at")),
        ("Highest UGR (camera axis)", sm.get("ugr_max")), ("  at position", sm.get("ugr_max_position")),
        ("Positions rated", sm.get("n_rated")), ("  above UGR 19", sm.get("n_over_19")),
        ("  above UGR 22", sm.get("n_over_22")),
        ("Check position: UGR change", sm.get("check_delta_ugr")),
        ("Zero", "optical (on the fiducial)" if study.get("zero") else "set by hand (no optical zero)"),
        ("  correction (pan, tilt °)", json.dumps((study.get("zero") or {}).get("correction_deg"))
         if study.get("zero") else None),
        ("Drift at the end (°)", (study.get("drift") or {}).get("offset_deg")),
        ("  limit (°)", (study.get("drift") or {}).get("limit_deg")),
        ("L_b (cd/m²)", sweep.get("L_b")), ("  from", "hemisphere sweep" if sweep else "each frame (provisional)"),
        ("  sweep coverage (cosine-weighted)", sweep.get("coverage_cos_weighted")),
        ("Forward-gaze UGR", "not built yet (REQUIREMENTS §3.6)"),
        ("k (cd/m² per DN·N²/(t·ISO))", proc.get("k")),
        ("Software", (study.get("software") or {}).get("commit")),
    ]
    for r, (k, v) in enumerate(items):
        ws.write(r, 0, k)
        if v is not None:
            ws.write(r, 1, v)
    r = len(items) + 1
    ws.write(r, 0, "Flags")
    for i, flag in enumerate(study.get("flags") or ["none"]):
        ws.write(r + i, 1, flag)
    ws.write(r + max(1, len(study.get("flags") or [])) + 1, 0,
             "UGR values are never averaged: the scale is logarithmic.")

    # -- Positions ------------------------------------------------------------ #
    thr = []
    for _, res in rows:
        if res:
            thr = [t["threshold_cd_m2"] for t in res.get("metrics", {}).get("threshold_sensitivity", [])]
            break
    cols = [("Position", 20, None), ("Pan °", 8, n2), ("Tilt °", 8, n2), ("UGR", 8, n1),
            ("Category", 16, None), ("L_b cd/m²", 10, n2), ("L_b from", 10, None),
            ("Sources", 8, None), ("Max L cd/m²", 12, sci), ("L50", 9, n2), ("L90", 9, n2),
            ("L99", 10, n1), ("Above threshold", 10, pct), ("Direct E at eye lx", 11, n2)]
    cols += [(f"UGR at {t:g} cd/m²", 10, n1) for t in thr]
    cols += [("Masks", 26, None), ("Flags", 60, wrap)]
    data = []
    for p, res in rows:
        r = res or {}
        m = r.get("metrics", {})
        sens = {t["threshold_cd_m2"]: t["ugr"] for t in m.get("threshold_sensitivity", [])}
        data.append([p["name"], p.get("pan_deg"), p.get("tilt_deg"), r.get("ugr"),
                     r.get("category") or p.get("state"), r.get("L_b"),
                     (r.get("background") or {}).get("method"), len(r.get("sources", [])) if r else None,
                     m.get("L_max_cd_m2"), m.get("L50_cd_m2"), m.get("L90_cd_m2"),
                     m.get("L99_cd_m2"), m.get("fraction_above_threshold"),
                     m.get("e_eye_direct_lx")] + [sens.get(t) for t in thr]
                    + [(r.get("provenance", {}).get("masks") or {}).get("version") or None,
                       "; ".join(r.get("flags", [])) or (p.get("error") or "")])
    ws = wb.add_worksheet("Positions")
    table(ws, cols, data)
    ugr_colours(ws, 3, 1, len(data))
    for i in range(len(thr)):
        ugr_colours(ws, 14 + i, 1, len(data))

    # -- Sources ----------------------------------------------------------------- #
    cols = [("Position", 20, None), ("Id", 5, None), ("Name", 14, None), ("Kind", 7, None),
            ("Type", 10, None), ("L mean cd/m²", 12, n1), ("L median", 11, n1),
            ("L peak", 11, n1), ("ω sr", 11, sci), ("σ °", 7, n1), ("τ °", 7, n1),
            ("p", 7, n2), ("L²ω/p²", 11, sci), ("Share", 8, pct), ("UGR change without it", 11, n2),
            ("E at eye lx", 10, n3), ("L / L_b", 9, n1), ("Clipped", 8, pct), ("Flags", 50, wrap)]
    data = []
    for p, res in rows:
        for s in (res or {}).get("sources", []):
            data.append([p["name"], s["id"], s["name"], s["kind"], s.get("source_type"),
                         s["L_cd_m2"], s.get("L_median_cd_m2"), s["L_peak_cd_m2"], s["omega_sr"],
                         s.get("sigma_deg"), s.get("tau_deg"), s.get("p"), s.get("contribution"),
                         s.get("share"), s.get("delta_ugr"), s.get("e_eye_lx"), s.get("contrast"),
                         s.get("clipped_fraction"), "; ".join(s.get("flags", []))])
    table(wb.add_worksheet("Sources"), cols, data)

    # -- Movement profile ------------------------------------------------------------ #
    ws = wb.add_worksheet("Movement profile")
    prof = study["profile"]
    ws.set_column(0, 0, 22, key)
    ws.set_column(1, 1, 60, wrap)
    for r, (k, v) in enumerate([("Profile", prof["name"]), ("Vehicle", prof.get("vehicle")),
                                ("Zero reference", prof.get("zero_reference")),
                                ("Forward gaze (pan, tilt °)", prof.get("forward_gaze")),
                                ("Profile hash", prof.get("content_hash"))]):
        ws.write(r, 0, k)
        if v is not None:
            ws.write(r, 1, json.dumps(v) if isinstance(v, list) else v)
    end = table(ws, [("Position", 22, None), ("Id", 12, None), ("Pan °", 9, n3),
                     ("Tilt °", 9, n3), ("Masks", 28, None), ("Aim off reference °", 12, n3),
                     ("Aim correlation", 12, n3)],
                [[p["name"], p["id"], p["pan_deg"], p["tilt_deg"], p.get("masks"),
                  (p.get("aim") or {}).get("total_deg"), (p.get("aim") or {}).get("peak")]
                 for p in study.get("positions", [])], row0=6)
    frames = (study.get("sweep") or {}).get("frames") or []
    if frames:
        ws.write(end + 1, 0, "Hemisphere sweep")
        table(ws, [("Frame", 22, None), ("Pan °", 12, n3), ("Tilt °", 9, n3)],
              [[f["bracket"], f["pan_deg"], f["tilt_deg"]] for f in frames], row0=end + 2)

    # -- Camera profile ----------------------------------------------------------- #
    ws = wb.add_worksheet("Camera profile")
    ws.set_column(0, 0, 34, key)
    ws.set_column(1, 1, 70, wrap)
    first = next((res for _, res in rows if res), None) or {}
    prov = first.get("provenance", {})
    cfg = prov.get("analysis_config", {})
    cap = prov.get("capture_settings", {})
    geo = first.get("geometry", {})
    items = [("Camera", prov.get("camera")), ("Capture settings", cap),
             ("Lens", cfg.get("lens")), ("Sensor", cfg.get("sensor")),
             ("Field of view °", [geo.get("fov_h_deg"), geo.get("fov_v_deg")]),
             ("Detection", cfg.get("detection")), ("UGR settings", cfg.get("ugr")),
             ("Background", cfg.get("background")), ("Report", cfg.get("report")),
             ("Camera profiles", "not built yet: settings are config/camera.toml and analysis.toml")]
    for r, (k, v) in enumerate(items):
        ws.write(r, 0, k)
        ws.write(r, 1, v if isinstance(v, str) else json.dumps(v))

    # -- Calibration & QA ----------------------------------------------------------- #
    ws = wb.add_worksheet("Calibration & QA")
    ws.set_column(0, 0, 34, key)
    ws.set_column(1, 1, 70, wrap)
    steps = {s["key"]: s for s in study.get("steps", [])}
    items = [("k", proc.get("k")), ("k obtained", (steps.get("calibration") or {}).get("detail")),
             ("Config overrides", proc.get("overrides")), ("Processing notes", proc.get("notes")),
             ("Zero", (steps.get("zero") or {}).get("detail")),
             ("Check position: UGR change", sm.get("check_delta_ugr")),
             ("Sweep frames", sweep.get("frames")), ("Sweep coverage", sweep.get("coverage_cos_weighted")),
             ("E_ind lx", sweep.get("E_ind_lx"))]
    for r, (k, v) in enumerate(items):
        ws.write(r, 0, k)
        if v is not None:
            ws.write(r, 1, v if isinstance(v, (str, int, float)) else json.dumps(v))
    data = []
    for p, res in rows:
        st = (res or {}).get("hdr_stats", {})
        ratios = [abs(x["ratio"] - 1) for x in st.get("pair_ratios", []) if x.get("ratio") is not None]
        data.append([p["name"], st.get("clipped_px"), max(ratios) if ratios else None,
                     (res or {}).get("timing_s", {}).get("total")])
    table(ws, [("Position", 22, None), ("Clipped px", 10, None),
               ("Worst exposure step off", 14, pct), ("Processing s", 12, n2)], data,
          row0=len(items) + 2)

    # -- Provenance ------------------------------------------------------------- #
    data = []
    for p, res in rows:
        prov = (res or {}).get("provenance", {})
        for f in prov.get("frames", []):
            data.append([p["name"], f["file"], f["shutter"], f["t_s"], f["sha256"],
                         prov.get("bracket_dir"), prov.get("processed_at")])
    ws = wb.add_worksheet("Provenance")
    end = table(ws, [("Position", 20, None), ("Frame", 14, None), ("Shutter", 9, None),
                     ("t effective s", 12, sci), ("SHA-256", 66, None), ("Bracket", 40, None),
                     ("Processed", 22, None)], data)
    ws.write(end + 1, 0, "Software commit")
    ws.write(end + 1, 1, json.dumps(study.get("software")))
    ws.write(end + 2, 0, "Analysis config")
    ws.write(end + 2, 1, json.dumps(first.get("provenance", {}).get("analysis_config")))
    wb.close()
    return path
