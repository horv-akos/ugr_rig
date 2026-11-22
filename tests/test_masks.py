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


def test_glazing_by_lighting(tmp_path):
    """Daylight: glazing behaves like an exclusion. Dark: what is bright in it is a
    reflection, rated with its own threshold and merge radius, and named R1, R2..."""
    import dataclasses

    base = load_analysis_config()
    cam = FakeCamera(load_camera_config(), scene=demo_scene(h=200, w=300), seed=1)
    cam.connect()
    cam.capture_bracket(tmp_path / "b", meta={"region": "demo", "pan_deg": 0, "tilt_deg": 0})

    as_exclude = detection.RegionMasks.load(_write(tmp_path / "x.json", DEMO_MASKS))
    glazing = dict(DEMO_MASKS, glazing=DEMO_MASKS["exclude"], exclude=[])
    as_glazing = detection.RegionMasks.load(_write(tmp_path / "g.json", glazing))

    def run(masks, out, **det):
        cfg = dataclasses.replace(base, detection=dataclasses.replace(base.detection, **det))
        return region.evaluate_region(tmp_path / "b", tmp_path / out, cfg, masks=masks,
                                      k=0.0064, images=False)

    a = run(as_exclude, "oa", lighting="daylight")
    day = run(as_glazing, "od", lighting="daylight")
    assert [s.name for s in a.sources] == [s.name for s in day.sources]
    assert a.ugr == pytest.approx(day.ugr)
    assert day.detection_info["glazing"] == "excluded"
    assert all(s.path == "direct" for s in day.sources)

    night = run(as_glazing, "on", lighting="dark", reflection_threshold_cd_m2=200.0)
    refl = [s for s in night.sources if s.path == "reflection"]
    assert refl and all(s.name.startswith("R") for s in refl)
    assert night.detection_info["n_reflections"] == len(refl)
    assert night.detection_info["reflection_threshold_cd_m2"] == 200.0
    assert night.ugr > day.ugr                      # the reflections count
    assert not any("reflection threshold not set" in f for f in night.flags)
    same = run(as_glazing, "os", lighting="dark")   # threshold not set apart: flagged
    assert any("reflection threshold not set" in f for f in same.flags)


def test_reflection_roi_inherits_its_luminaire(tmp_path):
    """An inclusion ROI with `reflects` is that luminaire's reflection: path reflection,
    the parent's type and spectral factor, the reflection threshold."""
    import numpy as np
    from photometry.masks import CameraGeometry

    L = np.full((60, 80), 5.0, np.float32)
    L[10:14, 10:14] = 9000.0                        # the luminaire
    L[40:44, 50:54] = 300.0                         # its reflection in the glass
    geom = CameraGeometry(width=80, height=60, pitch_mm=0.3, focal_mm=18.0)
    masks = detection.RegionMasks(
        glazing=[[(0.5, 0.5), (1.0, 0.5), (1.0, 1.0), (0.5, 1.0)]],
        include=[detection.InclusionROI("dome", [(0.05, 0.1), (0.25, 0.1), (0.25, 0.3), (0.05, 0.3)],
                                        source_type="amber"),
                 detection.InclusionROI("dome in glass", [(0.55, 0.6), (0.75, 0.6), (0.75, 0.8),
                                                          (0.55, 0.8)], reflects="dome")])
    factor = {"amber": 1.08}.get
    srcs, _, info = detection.detect_sources(
        L, np.zeros(L.shape, bool), geom, masks, 500.0, 4, 3, lambda t: factor(t, 1.0),
        "dark", reflection_threshold=200.0)
    by = {s.name: s for s in srcs}
    r = by["dome in glass"]
    assert r.path == "reflection" and r.reflects == "dome" and r.source_type == "amber"
    assert r.spectral_factor == 1.08 and by["dome"].path == "direct"
    assert info["n_reflections"] == 1
    # an edge-touching source is cut and flagged
    L[0:3, 70:80] = 9000.0
    srcs, _, _ = detection.detect_sources(L, np.zeros(L.shape, bool), geom, masks, 500.0, 4, 3,
                                          lambda t: 1.0, "dark", 200.0)
    cut = [s for s in srcs if s.cut]
    assert len(cut) == 1 and any("cut by the frame edge" in f for f in cut[0].flags)


def _write(path, obj):
    path.write_text(json.dumps(obj))
    return path
