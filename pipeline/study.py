"""Processing for a whole study, run in a worker process.

The rig server records; this processes. Each job is a plain dict and each function returns
one, so they cross a process boundary unchanged: processing a bracket takes seconds of numpy
on the Pi 4, and run in the server process it would stall live view and the jog heartbeat.

    process_sweep(job)   hemisphere sweep brackets -> E_ind, L_b (background.json)
    process_region(job)  one bracket -> UGR, sources, metrics, images (result.json in its folder)
    write_report(job)    the study folder -> report.xlsx
    summarise(study)     per-position results -> the study's summary numbers

`overrides` adjust the analysis config for one study without touching config/analysis.toml:
the simulated camera shoots small frames, so it passes the pixel pitch that keeps the real
2000D field of view. Overrides end up in the config snapshot of every result.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

from analysis import detection
from photometry.profile import AnalysisConfig, load_analysis_config

from . import region


def config(overrides: dict[str, Any] | None = None) -> AnalysisConfig:
    """config/analysis.toml with "section.field": value overrides applied."""
    cfg = load_analysis_config()
    for key, value in (overrides or {}).items():
        section, name = key.split(".", 1)
        part = getattr(cfg, section)
        if isinstance(value, list):
            value = tuple(value)
        cfg = dataclasses.replace(cfg, **{section: dataclasses.replace(part, **{name: value})})
    return cfg


def process_sweep(job: dict[str, Any]) -> dict[str, Any]:
    cfg = config(job.get("overrides"))
    out = region.background_from_sweep([Path(d) for d in job["brackets"]], cfg, k=job["k"])
    out["line_of_sight"] = list(cfg.ugr.line_of_sight) if cfg.ugr.line_of_sight != "camera" \
        else "camera"
    Path(job["out"]).write_text(json.dumps(out, indent=2))
    return out


def process_region(job: dict[str, Any]) -> dict[str, Any]:
    cfg = config(job.get("overrides"))
    masks = detection.RegionMasks.load(job.get("masks"))
    res = region.evaluate_region(job["bracket"], job["out"], cfg, masks=masks,
                                 lb=job.get("lb"), k=job["k"], name=job["name"],
                                 study=job.get("study"))
    sources = sorted(res.sources, key=lambda s: -s.contribution)
    return {
        "ugr": res.ugr,
        "category": res.category,
        "L_b": res.L_b,
        "lb_method": res.background.get("method"),
        "n_sources": len(res.sources),
        "flags": res.flags,
        "sources": [{"name": s.name, "kind": s.kind, "type": s.source_type,
                     "L_cd_m2": s.L_cd_m2, "L_peak_cd_m2": s.L_peak_cd_m2,
                     "omega_sr": s.omega_sr, "p": s.p, "contribution": s.contribution,
                     "share": s.share, "delta_ugr": s.delta_ugr, "e_eye_lx": s.e_eye_lx,
                     "contrast": s.contrast, "L_median_cd_m2": s.L_median_cd_m2,
                     "flags": s.flags} for s in sources],
        "outputs": dict(res.outputs),
        "timing_s": res.timing_s,
        "metrics": res.metrics,
        "masks_version": masks.version or None,
    }


def process_calibration(job: dict[str, Any]) -> dict[str, Any]:
    """k from a bracket of the matt target: the ROI's mean signal against the meter's
    luminance (photometry.calibration.measure_k). Raises on clipping or no signal."""
    cfg = config(job.get("overrides"))
    m = region.calibrate(job["bracket"], cfg, tuple(job["roi"]), float(job["target_cd_m2"]))
    return m.as_dict()


def write_report(job: dict[str, Any]) -> str:
    from report import excel

    return str(excel.write(job["dir"], job["study"]))


def summarise(study: dict[str, Any]) -> dict[str, Any]:
    """Headline numbers. UGR values are never averaged: the scale is logarithmic."""
    rated = [p for p in study.get("positions", [])
             if p.get("result") and p["result"].get("ugr") is not None]
    out: dict[str, Any] = {"n_rated": len(rated), "ugr_max": None, "ugr_max_position": None,
                           "n_over_19": sum(p["result"]["ugr"] > 19 for p in rated),
                           "n_over_22": sum(p["result"]["ugr"] > 22 for p in rated),
                           "check_delta_ugr": None}
    if rated:
        top = max(rated, key=lambda p: p["result"]["ugr"])
        out["ugr_max"], out["ugr_max_position"] = top["result"]["ugr"], top["name"]
    check = study.get("check") or {}
    first = next((p for p in study.get("positions", []) if p["id"] == check.get("id")), None)
    if (check.get("result") or {}).get("ugr") is not None and first \
            and (first.get("result") or {}).get("ugr") is not None:
        out["check_delta_ugr"] = check["result"]["ugr"] - first["result"]["ugr"]
    return out
