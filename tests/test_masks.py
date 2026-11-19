"""Per-position masks and reference images: storage, validation, API, and how detection
treats glazing until reflection detection exists."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from analysis import detection
from hal.camera_config import load_camera_config
from hal.camera_sim import DEMO_MASKS, FakeCamera, demo_scene
from photometry.profile import load_analysis_config
from pipeline import region
from service.controller import RigController
from service.positions import (PositionProfile, ProfileError, check_name, clean_masks,
                               derived_id)
from web.api import create_app

SQUARE = [[0.1, 0.1], [0.3, 0.1], [0.3, 0.3], [0.1, 0.3]]


@pytest.fixture
def client(tmp_path):
    r = RigController.from_config("sim", tmp_path, camera="sim", time_scale=0.0)
    c = TestClient(create_app(r))
    c.post("/api/mount/enable")
    c.post("/api/mount/zero", json={"wait": True})
    c.post("/api/positions", json={"name": "cab"})
    yield c, tmp_path
    r.close()


def test_clean_masks_validates():
    m = clean_masks({"exclude": [SQUARE], "glazing": [SQUARE],
                     "include": [{"name": "dome", "type": "white_led", "polygon": SQUARE,
                                  "threshold_cd_m2": 300}], "junk": 1})
    assert set(m) == {"exclude", "glazing", "include"}
    assert m["include"][0]["threshold_cd_m2"] == 300.0
    bad = [
        {"exclude": [SQUARE[:2]]},                                            # 2 points
        {"glazing": [[[0, 0], [1.2, 0], [1, 1]]]},                            # outside 0..1
        {"include": [{"name": "", "polygon": SQUARE}]},                       # no name
        {"include": [{"name": "a", "polygon": SQUARE}, {"name": "a", "polygon": SQUARE}]},
        {"include": [{"name": "a", "polygon": SQUARE, "threshold_cd_m2": -5}]},
        {"exclude": [SQUARE] * 101},
    ]
    for raw in bad:
        with pytest.raises(ProfileError):
            clean_masks(raw)


def test_names_cannot_hold_slashes():
    for name in ("roof / dome", "a\\b"):
        with pytest.raises(ProfileError):
            check_name(name, "position")


def test_old_profiles_get_stable_ids():
    text = 'schema = 1\nname = "old"\n\n[[position]]\nname = "dash"\npan_deg = 1.0\ntilt_deg = 2.0\n'
    a, b = PositionProfile.from_toml(text), PositionProfile.from_toml(text)
    assert a.positions[0].id == b.positions[0].id == derived_id("dash")


def test_save_takes_a_reference_image_and_masks_round_trip(client):
    c, data = client
    res = c.post("/api/positions/cab/save", json={"name": "dash", "wait": True}).json()
    assert res["motion"]["result"]["reference_image"]
    pos = c.get("/api/positions/cab").json()["positions"][0]
    assert pos["has_image"] and pos["masks"]["version"] is None
    img = c.get("/api/positions/cab/dash/image")
    assert img.status_code == 200 and img.content[:2] == b"\xff\xd8"

    doc = c.put("/api/positions/cab/dash/masks", json={
        "glazing": [SQUARE], "include": [{"name": "display", "type": "display", "polygon": SQUARE}],
    }).json()
    assert doc["version"].startswith("cab/dash@") and doc["position"]["name"] == "dash"
    assert c.get("/api/positions/cab/dash/masks").json()["version"] == doc["version"]
    assert c.get("/api/positions/cab").json()["positions"][0]["masks"] == {
        "exclude": 0, "glazing": 1, "include": 1, "version": doc["version"]}
    r = c.put("/api/positions/cab/dash/masks", json={"exclude": [SQUARE[:2]]})
    assert r.status_code == 422

    # The masks file is what `ugrctl process --masks` reads.
    files = list((data / "profiles" / "positions" / "cab").glob("*.masks.json"))
    loaded = detection.RegionMasks.load(files[0])
    assert len(loaded.glazing) == 1 and loaded.include[0].name == "display"

    # Re-saving the position under the same name keeps its masks attached.
    c.post("/api/mount/nudge", json={"direction": "right", "deg": 1, "wait": True})
    c.post("/api/positions/cab/save", json={"name": "dash", "wait": True})
    assert c.get("/api/positions/cab/dash/masks").json()["version"] == doc["version"]


def test_retake_needs_the_head_at_the_position(client):
    c, _ = client
    c.post("/api/positions/cab/save", json={"name": "dash", "wait": True})
    assert c.post("/api/positions/cab/dash/image").status_code == 200
    c.post("/api/mount/nudge", json={"direction": "up", "deg": 1, "wait": True})
    r = c.post("/api/positions/cab/dash/image")
    assert r.status_code == 409 and "go to" in r.json()["detail"]


def test_delete_moves_files_to_the_trash(client):
    c, data = client
    c.post("/api/positions/cab/save", json={"name": "dash", "wait": True})
    c.put("/api/positions/cab/dash/masks", json={"exclude": [SQUARE]})
    c.delete("/api/positions/cab/dash")
    root = data / "profiles" / "positions"
    assert not list((root / "cab").glob("*"))
    assert len(list((root / ".trash" / "cab").glob("*"))) == 2
    c.delete("/api/positions/cab")
    assert not (root / "cab").exists() and not (root / "cab.toml").exists()


def test_edit_profile_details(client):
    c, _ = client
    p = c.patch("/api/positions/cab", json={"zero_reference": " fiducial on the B-pillar "}).json()
    assert p["zero_reference"] == "fiducial on the B-pillar" and p["name"] == "cab"


def test_glazing_is_excluded_and_flagged(tmp_path):
    """Until reflection detection exists, glazing behaves like an exclusion (the daylight
    rule) and every result that has glazing says so."""
    cfg = load_analysis_config()
    cam = FakeCamera(load_camera_config(), scene=demo_scene(h=200, w=300), seed=1)
    cam.connect()
    cam.capture_bracket(tmp_path / "b", meta={"region": "demo", "pan_deg": 0, "tilt_deg": 0})

    as_exclude = detection.RegionMasks.load(_write(tmp_path / "x.json", DEMO_MASKS))
    glazing = dict(DEMO_MASKS, glazing=DEMO_MASKS["exclude"], exclude=[])
    as_glazing = detection.RegionMasks.load(_write(tmp_path / "g.json", glazing))
    a = region.evaluate_region(tmp_path / "b", tmp_path / "oa", cfg, masks=as_exclude,
                               k=0.0064, heatmap=False)
    b = region.evaluate_region(tmp_path / "b", tmp_path / "ob", cfg, masks=as_glazing,
                               k=0.0064, heatmap=False)
    assert [s.name for s in a.sources] == [s.name for s in b.sources]
    assert a.ugr == pytest.approx(b.ugr)
    assert any("glazing treated as excluded" in f for f in b.flags)
    assert not any("glazing" in f for f in a.flags)
    assert b.detection_info["n_glazing"] == 1


def _write(path, obj):
    path.write_text(json.dumps(obj))
    return path
