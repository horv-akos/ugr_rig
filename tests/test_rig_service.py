"""Rig controller and HTTP API against the simulated head (FastAPI TestClient)."""

from __future__ import annotations

import time
import tomllib
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from hal.backend import SimBackend
from hal.config import load_config
from hal.mount import PanTiltMount
from service.controller import RigController
from service.positions import PositionProfile, ProfileStore
from web.api import create_app


@pytest.fixture
def rig(tmp_path):
    def make(time_scale: float = 0.0) -> tuple[RigController, TestClient]:
        cfg = replace(load_config(), settle_ms=0)
        r = RigController(PanTiltMount(cfg, SimBackend(time_scale=time_scale)), tmp_path)
        made.append(r)
        return r, TestClient(create_app(r))

    made: list[RigController] = []
    yield make
    for r in made:
        r.close()


def wait_idle(c: TestClient, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while (st := c.get("/api/status").json())["state"] != "idle":
        assert time.monotonic() < deadline, st
        time.sleep(0.02)
    return st


def test_errors_map_to_status_codes(rig):
    _, c = rig()
    r = c.post("/api/mount/goto", json={"pan": 1, "tilt": 0})
    assert r.status_code == 409 and r.json()["error"] == "MountError"
    c.post("/api/mount/enable")
    r = c.post("/api/mount/goto", json={"pan": 1, "tilt": 0})
    assert r.status_code == 409 and r.json()["error"] == "NotZeroed"
    assert c.post("/api/mount/nudge", json={"direction": "sideways"}).status_code == 422
    assert c.post("/api/mount/speed", json={"fraction": 2}).status_code == 422
    assert c.get("/api/positions/nothing").status_code == 404
    c.post("/api/mount/zero", json={"wait": True})
    r = c.post("/api/mount/goto", json={"pan": 999, "tilt": 0})
    assert r.status_code == 409 and r.json()["error"] == "SoftLimit"


def test_zero_goto_and_status(rig):
    _, c = rig()
    c.post("/api/mount/enable")
    st = c.post("/api/mount/zero", json={"wait": True}).json()["status"]
    assert st["zeroed"] and all(st["backlash_ok"].values())
    m = c.post("/api/mount/goto", json={"pan": 12.5, "tilt": 4, "wait": True}).json()["motion"]
    assert m["result"]["pan_deg"] == pytest.approx(12.5, abs=0.02)
    assert c.get("/api/status").json()["state"] == "idle"


def test_busy_and_stop(rig):
    _, c = rig(time_scale=0.2)
    c.post("/api/mount/enable")
    c.post("/api/mount/zero", json={"wait": True})
    c.post("/api/mount/goto", json={"pan": 80, "tilt": 0})
    r = c.post("/api/mount/goto", json={"pan": 0, "tilt": 0})
    assert r.status_code == 409 and r.json()["error"] == "RigBusy"
    time.sleep(0.2)
    c.post("/api/mount/stop")
    st = wait_idle(c)
    assert st["last_motion"]["result"]["stopped"] and st["certain"]
    assert st["last_motion"]["stop_reason"] == "stop"


def test_jog_dead_man(rig):
    _, c = rig(time_scale=0.2)
    c.post("/api/mount/enable")
    c.post("/api/mount/zero", json={"wait": True})
    c.post("/api/mount/jog/start", json={"direction": "right"})
    for _ in range(3):
        time.sleep(0.1)
        assert c.post("/api/mount/jog/ping").json()["jogging"]
    st = wait_idle(c)  # pings stop here: the watchdog must end the jog
    assert st["last_motion"]["stop_reason"] == "heartbeat lost"
    assert st["certain"] and st["pan_deg"] > 0
    assert not c.post("/api/mount/jog/ping").json()["jogging"]


def test_save_refused_where_backlash_cannot_be_taken_up(rig):
    _, c = rig()
    c.post("/api/mount/enable")
    c.post("/api/mount/zero", json={"wait": True})
    c.post("/api/positions", json={"name": "cab"})
    c.post("/api/mount/jog/start", json={"direction": "left"})  # instant: lands on the limit
    assert not wait_idle(c)["backlash_ok"]["pan"]
    r = c.post("/api/positions/cab/save", json={"name": "edge", "wait": True})
    assert r.status_code == 409 and "soft limit" in r.json()["detail"]
    assert c.get("/api/positions/cab").json()["positions"] == []


def test_positions_round_trip(rig, tmp_path):
    _, c = rig(time_scale=0.2)
    c.post("/api/mount/enable")
    c.post("/api/mount/zero", json={"wait": True})
    name = 'Truck "A" — night'
    assert c.post("/api/positions", json={"name": name, "zero_reference": "fiducial\nB-pillar"}
                  ).status_code == 201
    assert c.post("/api/positions", json={"name": name}).status_code == 422  # duplicate
    c.post("/api/mount/jog/start", json={"direction": "left"})  # runs until the dead-man
    jogged = wait_idle(c)
    assert not jogged["backlash_ok"]["pan"]
    save = c.post(f"/api/positions/{name}/save", json={"name": "dash", "wait": True}).json()
    assert save["motion"]["result"]["backlash_taken_up"]
    c.post(f"/api/positions/{name}/gaze")

    path = next((tmp_path / "profiles" / "positions").glob("*.toml"))
    raw = tomllib.loads(path.read_text(encoding="utf-8"))  # valid TOML, special chars intact
    assert raw["name"] == name and raw["zero_reference"] == "fiducial\nB-pillar"
    p = PositionProfile.from_toml(path.read_text(encoding="utf-8"))
    assert p.position("dash").pan_deg == pytest.approx(jogged["pan_deg"], abs=1e-3)
    assert p.forward_gaze is not None
    assert ProfileStore(path.parent).names() == [name]

    assert c.post(f"/api/positions/{name}/order", json={"names": ["x"]}).status_code == 422
    assert c.delete(f"/api/positions/{name}/nope").status_code == 404
    c.post("/api/mount/nudge", json={"direction": "right", "deg": 0.5, "wait": True})
    m = c.post(f"/api/positions/{name}/dash/goto", json={"wait": True}).json()["motion"]
    assert m["result"]["pan_deg"] == pytest.approx(p.position("dash").pan_deg, abs=0.02)
    moved = c.delete(f"/api/positions/{name}").json()["moved_to"]
    assert ".trash" in moved and c.get("/api/positions").json() == []


def test_restart_hint(rig, tmp_path):
    r, c = rig()
    c.post("/api/mount/enable")
    c.post("/api/mount/zero", json={"wait": True})
    c.post("/api/mount/goto", json={"pan": 5, "tilt": 1, "wait": True})
    r2, c2 = rig()  # a new process: same data dir, unknown head
    st = c2.get("/api/status").json()
    assert not st["zeroed"]
    assert st["last_known"]["pan_deg"] == pytest.approx(5, abs=0.02)
    assert "hint" in st["last_known"]["note"]
