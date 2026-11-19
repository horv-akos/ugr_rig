"""Live view: the simulated cab view, the shared grabber, and the head pose it follows."""

from __future__ import annotations

import time
from dataclasses import replace

import numpy as np
import pytest

from hal.backend import SimBackend
from hal.camera_config import load_camera_config
from hal.camera_sim import CabView, FakeCamera, demo_scene, rotation
from hal.config import load_config
from hal.mount import PanTiltMount
from photometry.masks import rotation as masks_rotation
from service.controller import sim_pose
from service.liveview import LiveView


def wait_for(cond, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


@pytest.mark.parametrize("pan,tilt", [(0, 0), (30, -10), (-120, 45), (170, -60)])
def test_sim_rotation_matches_photometry(pan, tilt):
    assert np.allclose(rotation(pan, tilt), masks_rotation(pan, tilt))


def test_cab_view_follows_the_head():
    view = CabView(size=(240, 160))
    centre = lambda img: img[80, 120]  # noqa: E731
    on = view.render(*CabView.FIDUCIAL)
    off = view.render(0.0, 0.0)
    assert on.shape == (160, 240, 3)
    assert centre(on).min() > 240          # fiducial centre dot on the optical axis
    assert centre(off).max() < 120
    jpeg = view.jpeg(1.0, 2.0)
    assert jpeg[:2] == b"\xff\xd8" and view.jpeg(1.0, 2.0) is jpeg   # cached while still


def test_sim_pose_ignores_the_datum():
    cfg = replace(load_config(), settle_ms=0)
    sim = SimBackend()
    pose = sim_pose(sim, cfg)
    m = PanTiltMount(cfg, sim)
    m.enable()
    m.nudge(pan_deg=4.0, tilt_deg=-2.0)
    before = pose()
    m.set_zero()                            # the datum moves, the head does not
    assert m.position() == (0.0, 0.0)
    assert pose() == pytest.approx(before) and before[0] == pytest.approx(4.0, abs=0.02)


class FlakyCamera:
    """Fails `fail` times, then serves numbered frames; records the live-view calls."""

    def __init__(self, fail: int = 0) -> None:
        self.fail = fail
        self.calls: list[str] = []
        self.n = 0

    def connect(self) -> None:
        self.calls.append("connect")

    def close(self) -> None:
        self.calls.append("close")

    def preview(self) -> bytes:
        if self.fail:
            self.fail -= 1
            raise RuntimeError("USB link lost")
        self.n += 1
        return b"\xff\xd8frame%d" % self.n

    def end_preview(self) -> None:
        self.calls.append("end_preview")


def test_frames_only_while_watched():
    cam = FlakyCamera()
    lv = LiveView(cam, "test", max_fps=50, idle_stop_s=0.1)
    try:
        time.sleep(0.15)
        assert cam.n == 0 and lv.status()["state"] == "idle"
        lv.subscribe()
        assert wait_for(lambda: cam.n >= 3)
        assert lv.status()["state"] == "running"
        seq, frame = lv.latest()
        assert frame.startswith(b"\xff\xd8")
        lv.unsubscribe()
        assert wait_for(lambda: "end_preview" in cam.calls)   # idle: live view left
        n = cam.n
        time.sleep(0.15)
        assert cam.n == n
        assert lv.snapshot(timeout=2.0) is not None            # a frame on demand
    finally:
        lv.close()


def test_paused_hands_over_the_camera():
    cam = FlakyCamera()
    lv = LiveView(cam, "test", max_fps=50)
    try:
        lv.subscribe()
        assert wait_for(lambda: cam.n >= 2)
        with lv.paused() as got:
            assert got is cam and cam.calls[-1] == "end_preview"
            assert lv.status()["state"] == "paused"
            n = cam.n
            time.sleep(0.15)
            assert cam.n == n                                  # nothing grabbed meanwhile
        assert wait_for(lambda: cam.n > n)                     # and it resumes
    finally:
        lv.close()


def test_camera_errors_are_reported_and_retried():
    cam = FlakyCamera(fail=2)
    lv = LiveView(cam, "test", max_fps=50, retry_s=0.05)
    try:
        lv.subscribe()
        assert wait_for(lambda: lv.status()["state"] == "error")
        assert "USB link lost" in lv.status()["error"]
        assert wait_for(lambda: lv.status()["state"] == "running")
        assert lv.status()["error"] is None and cam.calls.count("connect") >= 2
    finally:
        lv.close()


def test_no_camera():
    lv = LiveView(None)
    assert lv.status()["state"] == "off" and lv.snapshot(timeout=0.1) is None
    lv.close()


def test_fake_camera_preview_and_bracket_end_it(tmp_path):
    cam = FakeCamera(load_camera_config(), scene=demo_scene(h=60, w=90),
                     view=CabView(size=(120, 80)))
    cam.connect()
    assert cam.preview()[:2] == b"\xff\xd8" and cam.previewing
    cam.capture_bracket(tmp_path / "b")
    assert not cam.previewing
