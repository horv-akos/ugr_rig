"""Studies: storage, index, the session sequence on the simulator, analysis, and the API."""

from __future__ import annotations

import json
import time
import zipfile
from dataclasses import replace
from io import BytesIO

import pytest
from fastapi.testclient import TestClient

from pipeline import study as worker
from service.controller import RigController
from service.study import sweep_directions
from storage import StudyIndex, StudyNotFound, StudyStore
from web.api import create_app

SQUARE = [[0.0, 0.0], [1.0, 0.0], [1.0, 0.3], [0.0, 0.3]]


def make_rig(tmp_path, time_scale: float = 0.0) -> RigController:
    rig = RigController.from_config("sim", tmp_path, camera="sim", time_scale=time_scale)
    rig.mount.cfg = replace(rig.mount.cfg, settle_ms=0)
    rig.enable()
    rig.wait(rig.set_zero())
    rig.create_profile("cab", "sim")
    for name, pan, tilt in (("dash", 18, -14), ("roof", 0, 45)):
        rig.wait(rig.goto(pan, tilt))
        rig.wait(rig.save_position("cab", name))
    return rig


def finish(rig: RigController, sid: str, timeout: float = 60) -> dict:
    deadline = time.monotonic() + timeout
    while rig._runs[sid].analysing.is_set():
        assert time.monotonic() < deadline, "analysis did not finish"
        time.sleep(0.05)
    return rig.study(sid)


# -- pieces ------------------------------------------------------------------- #


def test_sweep_grid_stays_inside_the_limits():
    limits = {"pan": [-170, 170], "tilt": [-45, 60]}
    dirs = sweep_directions((63.6, 44.9), limits, (0.0, 0.0))
    assert len(dirs) == 9 and dirs[0] == (0.0, 0.0)
    assert all(-170 <= p <= 170 and -45 <= t <= 60 for p, t in dirs)
    near_limit = sweep_directions((63.6, 44.9), limits, (160.0, 50.0))
    assert len(set(near_limit)) == len(near_limit)        # clamped duplicates dropped
    assert all(p <= 170 and t <= 60 for p, t in near_limit)


def test_store_refuses_paths_outside_a_study(tmp_path):
    store = StudyStore(tmp_path)
    sid = store.new_id("cab")
    store.create(sid)
    store.save({"id": sid, "profile": {"name": "cab"}})
    (store.path(sid) / "brackets").mkdir()
    (store.path(sid) / "brackets" / "frame.npz").write_bytes(b"x" * 10)
    (store.path(sid) / "results").mkdir()
    (store.path(sid) / "results" / "luminance.npy").write_bytes(b"x")
    (store.path(sid) / "results" / "result.json").write_text("{}")
    for bad in ("../x", "..", "brackets/../../secret"):
        with pytest.raises(StudyNotFound):
            store.file(sid, bad)
    with pytest.raises(StudyNotFound):
        store.path("../etc")
    names = zipfile.ZipFile(BytesIO(store.results_zip(sid))).namelist()
    assert f"{sid}/results/result.json" in names and f"{sid}/study.json" in names
    assert not any("brackets" in n or n.endswith(".npy") for n in names)


def test_index_search_and_rebuild(tmp_path):
    store = StudyStore(tmp_path / "studies")
    for i, (op, veh) in enumerate((("anna", "truck A"), ("ben", "truck B"))):
        sid = store.new_id(f"p{i}")
        store.create(sid)
        store.save({"id": sid, "started_at": f"2026-11-1{i}", "status": "done", "lighting": "dark",
                    "operator": op, "profile": {"name": f"p{i}", "vehicle": veh},
                    "positions": [], "flags": [], "summary": {"ugr_max": 20.0 + i}})
    idx = StudyIndex(tmp_path / "index.sqlite")
    assert idx.rebuild(store) == 2
    assert [r["operator"] for r in idx.search()] == ["ben", "anna"]      # newest first
    assert [r["vehicle"] for r in idx.search("truck A")] == ["truck A"]
    idx.close()


def test_summary_never_averages():
    study = {"positions": [{"id": "a", "name": "a", "result": {"ugr": 18.0}},
                           {"id": "b", "name": "b", "result": {"ugr": 23.0}},
                           {"id": "c", "name": "c", "result": None}],
             "check": {"id": "a", "result": {"ugr": 18.3}}}
    s = worker.summarise(study)
    assert s["ugr_max"] == 23.0 and s["ugr_max_position"] == "b"
    assert (s["n_rated"], s["n_over_19"], s["n_over_22"]) == (2, 1, 1)
    assert s["check_delta_ugr"] == pytest.approx(0.3)


# -- a whole study on the simulator --------------------------------------------- #


def test_study_end_to_end(tmp_path):
    rig = make_rig(tmp_path)
    try:
        pos = rig.get_profile("cab").position("dash")
        rig.set_position_masks("cab", "dash", {"glazing": [SQUARE]})
        m, sid = rig.start_study("cab", "dark", operator="op", notes="n")
        rig.wait(m)
        st = finish(rig, sid)
        assert st["status"] == "done"
        assert {s["key"]: s["state"] for s in st["steps"]} == {
            "checks": "done", "zero": "skipped", "calibration": "skipped", "sweep": "done",
            "positions": "done", "check": "done", "analysis": "done"}
        assert st["sweep"]["result"]["frames"] == 9 and st["sweep"]["result"]["L_b"] > 0
        assert all(p["state"] == "done" and p["result"]["lb_method"] == "sweep"
                   for p in st["positions"])
        # the simulated cab differs by position, and the check repeats the first
        ugrs = [p["result"]["ugr"] for p in st["positions"]]
        assert ugrs[0] != ugrs[1]
        assert abs(st["summary"]["check_delta_ugr"]) < 0.1
        # masks frozen into the study, and their glazing flagged in that position's result
        dash = st["positions"][0]
        assert dash["masks"] == f"masks/{pos.id}.json"
        assert any("glazing" in f for f in dash["result"]["flags"])
        assert dash["result"]["masks_version"].startswith("cab/dash@")
        # provenance and files
        assert st["software"]["commit"] and st["processing"]["k"] > 0
        d = rig.studies.path(sid)
        assert (d / "profile.toml").is_file() and (d / "background.json").is_file()
        assert (d / dash["result"]["outputs"]["heatmap"]).is_file()
        result = json.loads((d / dash["result"]["dir"] / "result.json").read_text())
        assert result["provenance"]["analysis_config"]["sensor"]["pixel_pitch_um"] > 3.72
        assert rig.list_studies("op")[0]["id"] == sid
    finally:
        rig.close()


def test_stopped_study_keeps_what_it_captured(tmp_path):
    rig = make_rig(tmp_path, time_scale=0.05)
    try:
        m, sid = rig.start_study("cab", "daylight", sweep=False, check=False)
        deadline = time.monotonic() + 30
        while rig.study(sid)["positions"][0]["state"] in ("pending", "moving", "capturing"):
            assert time.monotonic() < deadline
            time.sleep(0.02)
        rig.stop()
        rig.wait(m)
        st = finish(rig, sid)
        assert st["status"] == "stopped"
        assert any("stopped" in f for f in st["flags"])
        assert any("no hemisphere sweep" in f for f in st["flags"])
        assert st["positions"][0]["result"]["lb_method"] == "frame"
        assert st["positions"][1]["state"] in ("pending", "moving")
    finally:
        rig.close()


def test_interrupted_study_is_marked_on_restart(tmp_path):
    store = StudyStore(tmp_path / "studies")
    sid = store.new_id("cab")
    store.create(sid)
    store.save({"id": sid, "status": "recording", "started_at": "x", "lighting": "dark",
                "profile": {"name": "cab"}, "positions": [], "flags": [], "summary": {}})
    rig = RigController.from_config("sim", tmp_path, camera="sim", time_scale=0.0)
    try:
        st = rig.study(sid)
        assert st["status"] == "interrupted" and any("interrupted" in f for f in st["flags"])
        assert rig.list_studies()[0]["status"] == "interrupted"
    finally:
        rig.close()


def test_study_api(tmp_path):
    rig = RigController.from_config("sim", tmp_path, camera="sim", time_scale=0.0)
    rig.mount.cfg = replace(rig.mount.cfg, settle_ms=0)
    try:
        c = TestClient(create_app(rig))
        c.post("/api/positions", json={"name": "cab"})
        r = c.post("/api/studies", json={"profile": "cab", "lighting": "dark"})
        assert r.status_code == 409                       # drivers off
        c.post("/api/mount/enable")
        c.post("/api/mount/zero", json={"wait": True})
        r = c.post("/api/studies", json={"profile": "cab", "lighting": "dark"})
        assert r.status_code == 409 and "no positions" in r.json()["detail"]
        assert c.post("/api/studies", json={"profile": "cab", "lighting": "dusk"}).status_code == 422
        c.post("/api/positions/cab/save", json={"name": "ahead", "wait": True})
        r = c.post("/api/studies", json={"profile": "cab", "lighting": "dark", "sweep": False,
                                         "check": False})
        assert r.status_code == 201
        sid = r.json()["study"]
        st = finish(rig, sid)
        assert c.get(f"/api/studies/{sid}").json()["status"] == "done"
        assert c.get("/api/studies").json()[0]["id"] == sid
        heat = st["positions"][0]["result"]["outputs"]["heatmap"]
        assert c.get(f"/api/studies/{sid}/files/{heat}").headers["content-type"] == "image/png"
        assert c.get(f"/api/studies/{sid}/files/..%2F..%2Fstate.json").status_code == 404
        assert c.get("/api/studies/nope").status_code == 404
        z = c.get(f"/api/studies/{sid}/results.zip")
        assert z.headers["content-type"] == "application/zip" and len(z.content) > 1000
    finally:
        rig.close()
