"""Metrics beyond the UGR, the report images, and the Excel workbook."""

from __future__ import annotations

import csv
import dataclasses
import math
import re
import zipfile
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from analysis import detection, metrics
from hal.camera_config import load_camera_config
from hal.camera_sim import DEMO_MASKS, FakeCamera, demo_scene
from photometry.profile import load_analysis_config
from pipeline import region
from report import excel


def src(contribution: float, L: float = 1000.0, omega: float = 1e-3, sigma: float = 0.0):
    return SimpleNamespace(contribution=contribution, L_cd_m2=L, omega_sr=omega, sigma_deg=sigma,
                           share=None, delta_ugr=None, e_eye_lx=None, contrast=None)


def test_shares_and_ugr_without_each_source():
    s = [src(300.0), src(100.0, sigma=60.0)]
    metrics.rate_extras(s, L_b=10.0)
    assert sum(x.share for x in s) == pytest.approx(1.0)
    full = 8 * math.log10(0.25 / 10 * 400)
    assert s[0].delta_ugr == pytest.approx(full - 8 * math.log10(0.25 / 10 * 100))
    assert s[1].e_eye_lx == pytest.approx(1000 * 1e-3 * 0.5)       # cos 60
    assert s[0].contrast == pytest.approx(100.0)
    one = [src(50.0)]
    metrics.rate_extras(one, L_b=10.0)
    assert one[0].share == 1.0 and one[0].delta_ugr is None         # nothing left without it


@pytest.fixture(scope="module")
def region_out(tmp_path_factory):
    d = tmp_path_factory.mktemp("region")
    cfg = load_analysis_config()
    h, w = 200, 300
    pitch = cfg.sensor.width_px * cfg.sensor.pixel_pitch_um / (2 * w)
    cfg = dataclasses.replace(cfg, sensor=dataclasses.replace(cfg.sensor, pixel_pitch_um=pitch))
    cam = FakeCamera(load_camera_config(), scene=demo_scene(h, w), seed=2)
    cam.connect()
    cam.capture_bracket(d / "b", meta={"region": "demo", "pan_deg": 0, "tilt_deg": 0})
    masks = detection.RegionMasks(
        exclude=[[tuple(p) for p in DEMO_MASKS["exclude"][0]]],
        include=[detection.InclusionROI("dome", [tuple(p) for p in DEMO_MASKS["include"][0]["polygon"]],
                                        "white_led")])
    res = region.evaluate_region(d / "b", d / "out", cfg, masks=masks, k=0.0064, study="S-1")
    return d / "out", res, cfg


def test_region_metrics(region_out):
    out, res, cfg = region_out
    m = res.metrics
    assert m["L50_cd_m2"] < m["L90_cd_m2"] < m["L99_cd_m2"] <= m["L_max_cd_m2"]
    assert 0 < m["fraction_above_threshold"] < 0.2
    sens = m["threshold_sensitivity"]
    assert [t["threshold_cd_m2"] for t in sens] == list(cfg.detection.sensitivity_cd_m2)
    at500 = next(t for t in sens if t["threshold_cd_m2"] == cfg.detection.threshold_cd_m2)
    assert at500["ugr"] == pytest.approx(res.ugr)
    assert sum(s.share for s in res.sources) == pytest.approx(1.0)
    rows = list(csv.DictReader(open(out / "sources.csv")))
    assert {"share", "delta_ugr", "L_median_cd_m2", "e_eye_lx", "contrast"} <= set(rows[0])


def test_images_are_written_dark_and_stamped(region_out):
    out, res, _ = region_out
    for key, name in (("photo", "photo.jpg"), ("luminance_map", "luminance.png"),
                      ("contribution_map", "contribution.png"), ("sources_map", "sources.png"),
                      ("masks_map", "masks.png")):
        assert res.outputs[key] == name
        im = Image.open(out / name).convert("RGB")
        assert im.width >= 300
        assert max(im.getpixel((im.width - 2, im.height - 2))) < 90    # dark, not white paper


def test_workbook_has_every_sheet(region_out, tmp_path):
    out, res, _ = region_out
    study = {"id": "S-1", "status": "done", "profile": {"name": "cab", "vehicle": "sim"},
             "lighting": "dark", "positions": [{"id": "p1", "name": "demo", "pan_deg": 0.0,
                                                "tilt_deg": 0.0, "result": {"dir": "res"}}],
             "summary": {"ugr_max": res.ugr}, "flags": ["a flag"], "steps": [],
             "sweep": {"frames": [], "result": None}, "processing": {"k": 0.0064}}
    (tmp_path / "res").mkdir()
    (tmp_path / "res" / "result.json").write_text((out / "result.json").read_text())
    path = excel.write(tmp_path, study)
    with zipfile.ZipFile(path) as z:
        book = z.read("xl/workbook.xml").decode()
        strings = z.read("xl/sharedStrings.xml").decode()
    names = re.findall(r'<sheet name="([^"]+)"', book)
    assert names == ["Summary", "Positions", "Sources", "Movement profile", "Camera profile",
                     "Calibration &amp; QA", "Provenance"]
    assert "UGR at 1000 cd/m²" in strings and "a flag" in strings
