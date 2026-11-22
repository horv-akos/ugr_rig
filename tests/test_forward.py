"""Forward-gaze rating: de-duplication, rating against the gaze, coverage, tiling; and the
study, the profile coverage and the masks around it."""

from __future__ import annotations

import math
import time
from dataclasses import replace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from analysis import forward as fw
from photometry.masks import position_index, rotation
from service.positions import ProfileError, clean_masks
from service.controller import RigController
from web.api import create_app

FOV = (63.6, 44.9)


def src(name, pan, tilt, at, L=2000.0, om=1e-3, cut=False, path="direct"):
    d = rotation(pan, tilt).T @ rotation(*at)[:, 2]
    return {"name": name, "direction_cam": list(d), "L_cd_m2": L, "omega_sr": om, "cut": cut,
            "path": path, "flags": ["cut by the frame edge: omega is truncated"] if cut else []}


def test_one_source_seen_twice_is_counted_once():
    obs = (fw.observations("A", 0, 0, [src("S1", 0, 0, (20, -10)), src("S2", 0, 0, (-5, 5))])
           + fw.observations("B", 25, -10, [src("S1", 25, -10, (20, -10), L=2100)]))
    out = fw.deduplicate(obs, 0.5, 0.25)
    assert sorted(s.name for s in out) == ["A: S2", "B: S1"]    # nearest its frame centre kept
    s1 = next(s for s in out if s.name == "B: S1")
    assert s1.others[0]["position"] == "A" and s1.others[0]["L_ratio"] == pytest.approx(2000 / 2100, abs=1e-4)
    assert not s1.flags                                         # 5 % apart: consistent


def test_views_that_disagree_are_flagged():
    obs = (fw.observations("A", 0, 0, [src("S1", 0, 0, (20, -10), L=1000)])
           + fw.observations("B", 25, -10, [src("S1", 25, -10, (20, -10), L=2000)]))
    (s,) = fw.deduplicate(obs, 0.5, 0.25)
    assert any("disagree" in f for f in s.flags)


def test_a_cut_view_is_replaced_by_a_whole_one():
    obs = (fw.observations("A", 0, 0, [src("S1", 0, 0, (20, -10), cut=True)])
           + fw.observations("B", 40, -10, [src("S1", 40, -10, (20, -10))]))
    (s,) = fw.deduplicate(obs, 0.5, 0.25)
    assert s.kept.position == "B" and not s.flags               # A was nearer, but cut
    (only,) = fw.deduplicate(fw.observations("A", 0, 0, [src("S1", 0, 0, (20, -10), cut=True)]),
                             0.5, 0.25)
    assert any("every view" in f for f in only.flags)


def test_two_sources_in_one_frame_are_never_merged():
    obs = fw.observations("A", 0, 0, [src("a", 0, 0, (3, 0)), src("b", 0, 0, (3.3, 0))])
    assert len(fw.deduplicate(obs, 0.5, 0.25)) == 2


def test_rating_against_the_gaze():
    obs = fw.observations("A", 0, 0, [src("low", 0, 0, (10, -20)), src("high", 0, 0, (10, 20))])
    out = fw.deduplicate(obs, 0.5, 0.25)
    gaze = (0.0, -5.0)
    u = fw.rate(out, gaze, 20.0, 3e-4, 0.1, 16.0)
    by = {s.kept.name: s for s in out}
    for name, at in (("low", (10, -20)), ("high", (10, 20))):
        d = rotation(*gaze).T @ rotation(*at)[:, 2]
        assert by[name].p == pytest.approx(float(position_index(d, 16.0)))
    assert by["low"].p < by["high"].p                     # below the line of sight counts more
    total = sum(2000.0 ** 2 * 1e-3 / s.p ** 2 for s in out)
    assert u == pytest.approx(8 * math.log10(0.25 / 20.0 * total))
    assert sum(s.share for s in out) == pytest.approx(1.0)
    behind = fw.deduplicate(fw.observations("B", 180, 0, [src("rear", 180, 0, (180, 0))]), 0.5, 0.25)
    assert fw.rate(behind, gaze, 20.0, 3e-4, 0.1, 16.0) is None
    assert any("behind" in f for f in behind[0].flags)


def test_coverage_is_glare_weighted():
    g = fw.hemisphere((0.0, 0.0), 16.0, 1.0)
    one = fw.coverage(g, fw.covered(g, [(0, 0)], FOV))
    up = fw.coverage(g, fw.covered(g, [(0, 50)], FOV))
    down = fw.coverage(g, fw.covered(g, [(0, -50)], FOV))
    assert 0.3 < one < 0.6
    assert down > up                       # below the line of sight weighs more than above
    nine = fw.coverage(g, fw.covered(g, [(p, t) for p in (-57, 0, 57) for t in (-40, 0, 40)], FOV))
    assert nine > 0.95


def test_tiling_reaches_the_target_inside_the_limits():
    limits = {"pan": [-170, 170], "tilt": [-45, 60]}
    t = fw.propose_tiling((0, -5), FOV, limits, 16.0, 0.9)
    assert t["reached"] and t["coverage"] >= 0.9
    assert all(-170 <= p <= 170 and -45 <= q <= 60 for p, q in t["frames"])
    done = fw.propose_tiling((0, -5), FOV, limits, 16.0, 0.9, existing=[tuple(f) for f in t["frames"]])
    assert done["frames"] == [] and done["reached"]
    assert fw.coverage_png((0, -5), FOV, [("a", 0, -5)], 16.0, proposed=t["frames"])[:4] == b"\x89PNG"


def test_masks_take_a_reflects_link():
    m = clean_masks({"include": [{"name": "dome in glass", "polygon": [[0, 0], [0.1, 0], [0.1, 0.1]],
                                     "reflects": "dome"}]})
    assert m["include"][0]["reflects"] == "dome"
    with pytest.raises(ProfileError):
        clean_masks({"include": [{"name": "x", "polygon": [[0, 0], [0.1, 0], [0.1, 0.1]],
                                     "reflects": "x"}]})


# -- on the simulator --------------------------------------------------------------- #


def rig_with_profile(tmp_path, gaze: bool) -> RigController:
    rig = RigController.from_config("sim", tmp_path, camera="sim", time_scale=0.0)
    rig.mount.cfg = replace(rig.mount.cfg, settle_ms=0)
    rig.enable()
    rig.wait(rig.nudge("right", 6.2))
    rig.wait(rig.nudge("down", 2.8))
    rig.wait(rig.optical_zero(), timeout=60)
    rig.create_profile("cab", "sim")
    for name, pan, tilt in (("dash", 18, -14), ("display", 26, -12)):   # overlapping frames
        rig.wait(rig.goto(pan, tilt))
        rig.wait(rig.save_position("cab", name))
    if gaze:
        rig.wait(rig.goto(-6, 3))
        rig.set_forward_gaze("cab")
    return rig


def run_study(rig: RigController, **kw) -> dict:
    m, sid = rig.start_study("cab", "dark", check=False, **kw)
    rig.wait(m, timeout=120)
    deadline = time.monotonic() + 120
    while rig._runs[sid].analysing.is_set():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    return rig.study(sid)


def test_study_forward_gaze_counts_each_source_once(tmp_path):
    rig = rig_with_profile(tmp_path, gaze=True)
    try:
        st = run_study(rig, sweep=False)
        f = st["forward"]
        assert f["ugr"] is not None and 0 < f["coverage"] < 1
        n_obs = sum(p["result"]["n_sources"] for p in st["positions"])
        assert f["n_observations"] == n_obs and f["n_sources"] < n_obs   # the display, once
        assert any(s["seen_from"] == 2 for s in f["sources"])
        assert f["lb_method"] == "frame median"
        assert any("provisional L_b" in x for x in st["flags"])
        assert f["lower_bound"] and any("lower bound" in x for x in st["flags"])  # 2 frames only
        assert st["summary"]["forward_ugr"] == f["ugr"]
        d = rig.studies.path(st["id"])
        assert (d / "forward.json").is_file() and (d / "coverage.png").is_file()
        import zipfile

        with zipfile.ZipFile(d / "report.xlsx") as z:
            assert "Forward gaze" in z.read("xl/workbook.xml").decode()
    finally:
        rig.close()


def test_study_without_a_gaze_is_flagged(tmp_path):
    rig = rig_with_profile(tmp_path, gaze=False)
    try:
        st = run_study(rig, sweep=False)
        assert st["forward"] is None
        assert any("no forward gaze" in x for x in st["flags"])
    finally:
        rig.close()


def test_profile_coverage_api(tmp_path):
    rig = rig_with_profile(tmp_path, gaze=True)
    try:
        c = TestClient(create_app(rig))
        r = c.get("/api/positions/cab/coverage").json()
        assert 0 < r["coverage"] < r["coverage_min"] and r["proposal"]["frames"]
        assert r["proposal"]["coverage"] > r["coverage"]
        png = c.get("/api/positions/cab/coverage.png")
        assert png.status_code == 200 and png.content[:4] == b"\x89PNG"
        rig.create_profile("bare", "sim")
        assert c.get("/api/positions/bare/coverage").status_code == 409   # no gaze yet
    finally:
        rig.close()


def test_static_files_are_revalidated(tmp_path):
    """After an update the pages must not run cached scripts against the new API."""
    rig = RigController.from_config("sim", tmp_path, camera="none")
    try:
        r = TestClient(create_app(rig)).get("/static/movement.js")
        assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
    finally:
        rig.close()
