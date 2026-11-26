"""Reports and housekeeping: the PDF, the raw zip, the comparison view, profile copy, the
shutdown button and rigctl's study commands. One simulated rig records two studies (dark,
then daylight) for the whole module."""

from __future__ import annotations

import io
import socket
import sys
import threading
import time
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from service.controller import RigController
from web.api import create_app


def _finish(rig: RigController, sid: str) -> dict:
    deadline = time.monotonic() + 120
    while rig._runs[sid].analysing.is_set():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    return rig.study(sid)


@pytest.fixture(scope="module")
def rig(tmp_path_factory):
    r = RigController.from_config("sim", tmp_path_factory.mktemp("rig"), camera="sim",
                                  time_scale=0.0)
    r.mount.cfg = replace(r.mount.cfg, settle_ms=0)
    r.enable()
    r.wait(r.nudge("right", 6.2))
    r.wait(r.nudge("down", 2.8))
    r.wait(r.optical_zero(), timeout=60)
    r.create_profile("cab", "sim")
    for name, pan, tilt in (("dash", 18, -14), ("display", 26, -12)):
        r.wait(r.goto(pan, tilt))
        r.wait(r.save_position("cab", name))
    r.set_position_masks("cab", "dash", {"glazing": [[[0, 0], [1, 0], [1, 0.2]]]})
    r.wait(r.goto(-6, 3))
    r.set_forward_gaze("cab")
    r.sids = []
    for lighting in ("dark", "daylight"):
        m, sid = r.start_study("cab", lighting, sweep=False, check=False)
        r.wait(m, timeout=120)
        _finish(r, sid)
        r.sids.append(sid)
    yield r
    r.close()


def test_pdf_report_is_written(rig):
    st = rig.study(rig.sids[0])
    assert st["outputs"]["pdf"] == "report.pdf" and st["outputs"]["excel"] == "report.xlsx"
    pdf = (rig.studies.path(st["id"]) / "report.pdf").read_bytes()
    assert pdf[:5] == b"%PDF-"
    # study, forward gaze, two positions, provenance
    import re

    assert len(re.findall(rb"/Type\s*/Page(?!s)", pdf)) == 5
    assert not any("PDF report" in f for f in st["flags"])
    z = zipfile.ZipFile(io.BytesIO(rig.studies.results_zip(st["id"])))
    assert f"{st['id']}/report.pdf" in z.namelist()


def test_raw_zip_streams_every_bracket(rig):
    sid = rig.sids[0]
    c = TestClient(create_app(rig))
    r = c.get(f"/api/studies/{sid}/raw.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert z.testzip() is None
    names = z.namelist()
    on_disk = [p for p in (rig.studies.path(sid) / "brackets").rglob("*") if p.is_file()]
    assert len(names) == len(on_disk) + 1 and f"{sid}/study.json" in names
    assert all(i.compress_type == zipfile.ZIP_STORED for i in z.infolist())
    assert c.get("/api/studies/nope/raw.zip").status_code == 404


def test_compare_lists_a_position_across_studies(rig):
    pos = rig.get_profile("cab").position("dash")
    c = TestClient(create_app(rig))
    r = c.get("/api/compare", params={"profile": "cab", "position": pos.id}).json()
    assert [x["study"] for x in r["rows"]] == rig.sids and r["mixed_lighting"]
    assert all(x["ugr"] is not None and x["image"] for x in r["rows"])
    fw = c.get("/api/compare", params={"profile": "cab"}).json()
    assert len(fw["rows"]) == 2 and all(x["coverage"] > 0 for x in fw["rows"])
    lib = c.get("/api/studies").json()
    assert all(x["ugr_forward"] is not None for x in lib)


def test_profile_copy_brings_images_and_masks(rig):
    c = TestClient(create_app(rig))
    p = c.post("/api/positions", json={"name": "cab 2", "copy_from": "cab", "vehicle": "sim 2"}).json()
    src = rig.get_profile("cab")
    assert p["vehicle"] == "sim 2" and p["forward_gaze"] == list(src.forward_gaze)
    assert [x["id"] for x in p["positions"]] == [x.id for x in src.positions]
    dash = rig.get_profile("cab 2").position("dash")
    assert rig.profiles.image_path("cab 2", dash).is_file()
    assert rig.profiles.load_masks("cab 2", dash)["glazing"]
    assert c.post("/api/positions", json={"name": "cab 3", "copy_from": "nope"}).status_code == 404


def test_shutdown_is_offered_only_on_the_rig(rig, tmp_path):
    c = TestClient(create_app(rig))
    sysinfo = c.get("/api/system").json()
    assert sysinfo["shutdown"] is False and sysinfo["health"]["server_threads"] >= 1
    assert c.post("/api/system/shutdown").status_code == 409


def test_shutdown_runs_the_command_after_releasing(tmp_path):
    marker = tmp_path / "powered-off"
    rig = RigController.from_config("sim", tmp_path / "data", camera="none")
    rig.enable()
    c = TestClient(create_app(rig, shutdown_command=[
        sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"]))
    assert c.get("/api/system").json()["shutdown"] is True
    r = c.post("/api/system/shutdown")
    assert r.status_code == 200 and not rig.mount.enabled          # drivers released first
    deadline = time.monotonic() + 10
    while not marker.exists():
        assert time.monotonic() < deadline, "the shutdown command did not run"
        time.sleep(0.1)


def test_rigctl_study_commands(rig, tmp_path, capsys):
    import uvicorn

    from tools import rigctl

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(rig), host="127.0.0.1", port=port,
                                           log_level="error"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        url = ["--url", f"http://127.0.0.1:{port}"]
        assert rigctl.main(url + ["study", "start", "cab", "--lighting", "dark", "--no-sweep",
                                  "--no-check", "--wait", "--timeout", "120"]) == 0
        out = capsys.readouterr().out
        assert "forward gaze UGR" in out and "done" in out
        assert rigctl.main(url + ["study", "list"]) == 0
        assert "forward" in capsys.readouterr().out
        sid = rig.list_studies()[0]["id"]
        assert rigctl.main(url + ["study", "show", sid]) == 0
        assert "Drift check" in capsys.readouterr().out
        dest = tmp_path / "r.pdf"
        assert rigctl.main(url + ["study", "download", sid, "pdf", "-o", str(dest)]) == 0
        assert dest.read_bytes()[:5] == b"%PDF-"
        raw = tmp_path / "raw.zip"
        assert rigctl.main(url + ["study", "download", sid, "raw", "-o", str(raw)]) == 0
        assert zipfile.ZipFile(raw).testzip() is None
        assert rigctl.main(url + ["shutdown", "--yes"]) == 1          # not offered here
        assert rigctl.main(url + ["positions", "copy", "cab", "cab copy"]) == 0
        assert "2 positions" in capsys.readouterr().out
    finally:
        server.should_exit = True
        t.join(timeout=10)
