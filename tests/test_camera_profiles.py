"""Camera profiles: TOML, validation, copies, and k from the matt target on the simulator."""

from __future__ import annotations

import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from service.camera_profiles import (CameraProfileStore, KRecord, ProfileError, from_config,
                                     parse_bracket)
from service.controller import RigController
from web.api import create_app

ROI = [0.45, 0.4, 0.55, 0.6]
K_TRUE = 0.0064


def rig_at_display(tmp_path) -> RigController:
    rig = RigController.from_config("sim", tmp_path, camera="sim", time_scale=0.0)
    rig.mount.cfg = replace(rig.mount.cfg, settle_ms=0)
    rig.enable()
    rig.wait(rig.set_zero())
    rig.wait(rig.goto(26, -16))          # the simulated display, 900 cd/m2, fills the ROI
    return rig


def calibrate(rig: RigController, **meter) -> dict:
    m = rig.wait(rig.calibrate(rig.camera_profile().name, ROI, **meter), timeout=60)
    assert not m["running"], "calibration did not finish"
    return m["result"]


# -- profile ------------------------------------------------------------------ #


def test_toml_round_trip_keeps_everything():
    p = replace(from_config(), name="2000D 18-55", notes='a "quoted" note',
                spectral_factors={"white_led": 1.0, "amber": 1.08}).with_k(
        KRecord(at="2026-11-20T10:00:00Z", k=0.0064, target_cd_m2=900.0,
                roi=tuple(ROI), cv=0.01, drift=None, calibration="c1"))
    q = type(p).from_toml(p.to_toml())
    assert q == p
    assert q.content_hash == p.content_hash


def test_validation_and_k_not_editable():
    p = from_config()
    with pytest.raises(ProfileError):
        p.edited({"focal_mm": 2.0})
    with pytest.raises(ProfileError):
        p.edited({"spectral_factors": {"amber": 9.0}})
    with pytest.raises(ProfileError, match="calibration"):
        p.edited({"k": 0.01})
    assert p.edited({"iso": "200"}).iso == 200
    assert parse_bracket("1/4, 1/60 ,1/1000") == ("1/4", "1/60", "1/1000")


def test_copy_starts_uncalibrated_and_delete_goes_to_trash(tmp_path):
    store = CameraProfileStore(tmp_path)
    store.ensure_default()
    base = store.names()[0]
    store.save(store.load(base).with_k(KRecord(at="2026-11-20T10:00:00Z", k=0.0064,
                                               target_cd_m2=900.0, roi=tuple(ROI), cv=0.01,
                                               drift=None, calibration="c1")))
    copy = store.create("new lens", copy_from=base)
    assert copy.k == 0.0 and copy.k_history == () and copy.focal_mm == store.load(base).focal_mm
    with pytest.raises(ProfileError):
        store.create("new lens")
    moved = store.delete("new lens")
    assert ".trash" in moved and "new lens" not in store.names()


def test_typed_k_replaces_and_keeps_history(tmp_path):
    p = from_config()
    p = replace(p, k=0.0).with_k(KRecord(at="2026-11-20T10:00:00Z", k=0.0064, target_cd_m2=900.0,
                                         roi=tuple(ROI), cv=0.01, drift=None))
    assert p.k_source == "target"
    with pytest.raises(ProfileError, match="comes from"):
        p.with_typed_k(0.007, "  ")
    with pytest.raises(ProfileError):
        p.with_typed_k(-1, "lab")
    q = p.with_typed_k(0.0070, "lab calibration 2026-11")
    assert q.k == 0.0070 and q.k_source == "typed" and q.k_note == "lab calibration 2026-11"
    assert q.k_history[-1].drift == pytest.approx(0.0070 / 0.0064 - 1)
    assert len(q.k_history) == 2 and q.k_history[0].k == 0.0064
    r = type(q).from_toml(q.to_toml())
    assert r == q and r.k_history[-1].roi is None
    assert replace(from_config(), k=0.005).k_source == "config"


# -- calibration on the simulator --------------------------------------------- #


def test_calibration_recovers_k_and_accept_records_it(tmp_path):
    rig = rig_at_display(tmp_path)
    try:
        rec = calibrate(rig, luminance=900.0)
        m = rec["measurement"]
        assert m["k"] == pytest.approx(K_TRUE, rel=0.02)
        assert m["k_stored"] in (0.0, None) and m["cv"] < 0.05
        assert rig.camera_profile().k == 0.0               # offered, not applied
        p = rig.accept_calibration(rig.camera_profile().name, rec["id"])
        assert p.k == m["k"] and len(p.k_history) == 1
        # lux meter on a panel: L = rho E / pi, the same target
        rec2 = calibrate(rig, lux=900.0 * 3.141592653589793 / 0.9, rho=0.9)
        assert rec2["measurement"]["k"] == pytest.approx(m["k"], rel=0.005)
        assert abs(rec2["measurement"]["drift"]) < 0.005
        assert [r["id"] for r in rig.calibrations(p.name)] == [rec2["id"], rec["id"]]
    finally:
        rig.close()


def test_bad_roi_and_meter_are_refused(tmp_path):
    rig = rig_at_display(tmp_path)
    try:
        name = rig.camera_profile().name
        with pytest.raises(ValueError):
            rig.calibrate(name, [0.6, 0.4, 0.5, 0.6], luminance=900)
        with pytest.raises(ValueError):
            rig.calibrate(name, ROI, luminance=0)
    finally:
        rig.close()


def test_study_counts_a_fresh_calibration(tmp_path):
    rig = rig_at_display(tmp_path)
    try:
        rig.create_profile("cab", "sim")
        rig.wait(rig.save_position("cab", "display"))
        rig.wait(rig.goto(0, 0))
        _, sid = rig.start_study("cab", "dark", sweep=False, check=False)
        steps = _wait_steps(rig, sid)
        assert steps["calibration"]["state"] == "skipped"       # never calibrated

        rec = calibrate(rig, luminance=900.0)
        rig.accept_calibration(rig.camera_profile().name, rec["id"])
        _, sid2 = rig.start_study("cab", "dark", sweep=False, check=False)
        steps = _wait_steps(rig, sid2)
        assert steps["calibration"]["state"] == "done"
        assert rig.study(sid2)["camera_profile"]["name"] == rig.camera_profile().name

        # a typed k overrides the fresh measurement: the step is skipped and the study flagged
        rig.set_k(rig.camera_profile().name, 0.0070, "lab calibration 2026-11")
        _, sid3 = rig.start_study("cab", "dark", sweep=False, check=False)
        steps = _wait_steps(rig, sid3)
        s3 = rig.study(sid3)
        assert steps["calibration"]["state"] == "skipped"
        assert "typed" in steps["calibration"]["detail"]
        assert s3["camera_profile"]["k_source"] == "typed"
        assert any("typed by hand" in str(f) for f in s3["flags"])
    finally:
        rig.close()


def _wait_steps(rig: RigController, sid: str, timeout: float = 60) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        s = rig.study(sid)
        steps = {x["key"]: x for x in s["steps"]}
        if steps["calibration"]["state"] not in ("pending", "running") \
                and rig.status()["state"] == "idle" and not rig._runs[sid].analysing.is_set():
            return steps
        assert time.monotonic() < deadline, "study did not finish"
        time.sleep(0.05)


# -- API ------------------------------------------------------------------------ #


def test_camera_api(tmp_path):
    rig = rig_at_display(tmp_path)
    try:
        c = TestClient(create_app(rig))
        lst = c.get("/api/camera/profiles").json()
        assert len(lst) == 1 and lst[0]["active"]
        name = lst[0]["name"]
        assert c.patch(f"/api/camera/profiles/{name}", json={"k": 1}).status_code in (400, 409, 422)
        assert c.patch(f"/api/camera/profiles/{name}", json={"focal_mm": 24}).json()["focal_mm"] == 24
        assert c.get(f"/api/camera/profiles/{name}/geometry.png").headers["content-type"] == "image/png"
        assert c.post("/api/camera/profiles", json={"name": "b", "copy_from": name}).json()["k"] == 0
        assert c.post("/api/camera/profiles/b/activate").status_code == 200
        assert {p["name"]: p["active"] for p in c.get("/api/camera/profiles").json()}["b"]
        assert c.get("/api/camera/info").json()["info"]
        r = c.post("/api/camera/calibrate", json={"profile": "b", "roi": ROI, "luminance": 900})
        assert r.status_code in (200, 202), r.text
        deadline = time.monotonic() + 60
        while not c.get("/api/camera/calibrations", params={"profile": "b"}).json():
            assert time.monotonic() < deadline
            time.sleep(0.1)
        while rig.status()["state"] != "idle":
            time.sleep(0.05)
        cid = c.get("/api/camera/calibrations", params={"profile": "b"}).json()[0]["id"]
        assert c.post("/api/camera/profiles/b/accept", json={"calibration": cid}).json()["k"] > 0
        assert c.post(f"/api/camera/profiles/{name}/accept", json={"calibration": cid}).status_code == 409
        assert c.post("/api/camera/profiles/b/k", json={"k": 0.007}).status_code == 422   # no note
        typed = c.post("/api/camera/profiles/b/k", json={"k": 0.007, "note": "lab"}).json()
        assert typed["k"] == 0.007 and typed["k_source"] == "typed" and typed["k_note"] == "lab"
        assert c.delete("/api/camera/profiles/b").status_code == 200
        assert c.delete(f"/api/camera/profiles/{name}").status_code == 409     # the last one
    finally:
        rig.close()
