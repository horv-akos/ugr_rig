"""Optical zero, drift check, aim check and lens geometry: the camera as the head's sensor."""

from __future__ import annotations

import time
from dataclasses import replace

import numpy as np
import pytest

from analysis.spot import SpotNotFound, find_spot, phase_correlate
from photometry import lensfit
from photometry.masks import CameraGeometry
from service import study as study_mod
from service.controller import RigController

SENSOR_W = 22.32


def gaussian(u0: float, v0: float, sigma: float = 1.5, shape=(120, 160), peak=200.0, bg=20.0):
    v, u = np.mgrid[0:shape[0], 0:shape[1]].astype(np.float32)
    return bg + peak * np.exp(-((u - u0) ** 2 + (v - v0) ** 2) / (2 * sigma ** 2))


# -- spots and shifts ------------------------------------------------------------ #


def test_spot_centroid_is_sub_pixel():
    for u0, v0 in [(80.0, 60.0), (80.37, 59.71), (73.5, 66.25)]:
        s = find_spot(gaussian(u0, v0), (80, 60), 20, 12, 40)
        assert abs(s.u - u0) < 0.05 and abs(s.v - v0) < 0.05   # 0.003 deg at 18 mm
        assert not s.saturated


def test_spot_refusals():
    with pytest.raises(SpotNotFound, match="nothing bright"):
        find_spot(gaussian(80, 60, peak=10), (80, 60), 20, 12, 40)
    with pytest.raises(SpotNotFound, match="not a small spot"):
        find_spot(gaussian(80, 60, sigma=8), (80, 60), 40, 12, 40)
    with pytest.raises(SpotNotFound, match="edge"):
        find_spot(gaussian(95, 60), (80, 60), 16, 12, 40)        # cut by the window


def test_phase_correlation_recovers_a_shift():
    rng = np.random.default_rng(3)
    base = rng.random((96, 128)).astype(np.float32)
    from scipy import ndimage

    base = ndimage.gaussian_filter(base, 2) * 255
    moved = ndimage.shift(base, (-2.25, 3.5), mode="wrap")     # rows, cols
    s = phase_correlate(base, moved)
    assert s.du == pytest.approx(3.5, abs=0.15) and s.dv == pytest.approx(-2.25, abs=0.15)
    assert s.peak > 0.3


# -- lens geometry --------------------------------------------------------------- #


def test_project_inverts_pixel_directions():
    g = CameraGeometry.for_frame(720, 480, SENSOR_W, 18.0, k1=-0.03)
    u = np.array([0.0, 100.5, 359.5, 719.0])
    v = np.array([0.0, 400.0, 239.5, 479.0])
    pu, pv = g.project(g.pixel_directions(u, v))
    assert np.allclose(pu, u, atol=1e-6) and np.allclose(pv, v, atol=1e-6)


def synthetic(f: float, k1: float, zero_tilt: float, spot=(0.2, -0.1), e_mm=0.0, d_m=1e6):
    g = CameraGeometry.for_frame(720, 480, SENSOR_W, f, k1)
    poses = np.array(lensfit.grid(g.fov_deg()))
    u, v = lensfit.predict(g, poses, spot[0], spot[1], d_m, e_mm, zero_tilt)
    return [lensfit.Sighting(p, t, a, b) for (p, t), a, b in zip(poses, u, v)]


def test_lens_fit_recovers_focal_k1_and_zero_tilt():
    s = synthetic(18.3, -0.02, 4.0)
    f = lensfit.fit(s, 720, 480, SENSOR_W, 18.0, 1e6)
    assert f.focal_mm == pytest.approx(18.3, abs=1e-3)
    assert f.k1 == pytest.approx(-0.02, abs=1e-4)
    assert f.zero_tilt_deg == pytest.approx(4.0, abs=0.01)
    assert (f.spot_pan_deg, f.spot_tilt_deg) == pytest.approx((0.2, -0.1), abs=1e-3)
    assert f.rms_px < 1e-3


def test_parallax_is_corrected_only_when_the_pupil_offset_is_known():
    s = synthetic(18.0, 0.0, 0.0, e_mm=60.0, d_m=2.0)
    known = lensfit.fit(s, 720, 480, SENSOR_W, 18.0, 2.0, pupil_offset_mm=60.0)
    unknown = lensfit.fit(s, 720, 480, SENSOR_W, 18.0, 2.0, pupil_offset_mm=0.0)
    assert known.focal_mm == pytest.approx(18.0, abs=1e-3)
    assert abs(unknown.focal_mm / 18.0 - 1) > 0.01        # ~e/D = 3 % too long


# -- on the simulator --------------------------------------------------------------- #


def zeroed_rig(tmp_path) -> RigController:
    rig = RigController.from_config("sim", tmp_path, camera="sim", time_scale=0.0)
    rig.mount.cfg = replace(rig.mount.cfg, settle_ms=0)
    rig.enable()
    rig.wait(rig.nudge("right", 5.0))         # near the simulated fiducial at (6, -3)
    rig.wait(rig.nudge("down", 2.5))
    return rig


def slip(rig: RigController, pan_deg: float) -> None:
    """The head turns without the step count knowing: lost steps, as the world sees them."""
    pin = rig.mount.cfg.pins.pan_step
    rig.mount._b.net_steps[pin] += round(pan_deg * rig.mount.cfg.pan.steps_per_deg)


def test_optical_zero_and_zero_check(tmp_path):
    rig = zeroed_rig(tmp_path)
    try:
        r = rig.wait(rig.optical_zero(), timeout=60)["result"]
        assert r["correction_deg"] == pytest.approx([1.0, -0.5], abs=0.05)
        assert r["residual_deg"] < 0.03
        assert rig.status()["zero"]["method"] == "optical"
        slip(rig, 0.3)
        c = rig.wait(rig.check_zero(), timeout=60)["result"]
        assert c["offset_deg"] == pytest.approx(0.3, abs=0.03) and c["over_limit"]
        assert rig.status()["zero_check"]["offset_deg"] == c["offset_deg"]
        rig.wait(rig.set_zero())                  # by hand: the method says so
        assert rig.status()["zero"]["method"] == "manual"
    finally:
        rig.close()


def test_lens_measured_with_the_head_and_accepted(tmp_path):
    rig = zeroed_rig(tmp_path)
    try:
        rig.wait(rig.optical_zero(), timeout=60)
        name = rig.camera_profile().name
        rec = rig.wait(rig.measure_lens(name, 1e6), timeout=300)["result"]
        f = rec["fit"]
        assert f["focal_mm"] == pytest.approx(18.0, abs=0.05)   # the simulator's lens
        assert abs(f["k1"]) < 0.01 and f["rms_px"] < 0.5 and f["n"] >= 20
        assert f["zero_tilt_deg"] == pytest.approx(-3.0, abs=0.15)
        assert any("pupil offset" in n for n in rec["notes"])
        p = rig.accept_lens(name, rec["id"])
        assert p.focal_mm == f["focal_mm"] and p.lens_measured_at == rec["at"]
        assert rig.lens_fits(name)[0]["accepted_at"]
        assert type(p).from_toml(p.to_toml()) == p
        # typing a focal length makes the lens nominal again
        q = p.edited({"focal_mm": 18.5})
        assert q.lens_measured_at == "" and q.lens_fit == ""
    finally:
        rig.close()


def test_study_catches_steps_lost_after_the_zero(tmp_path, monkeypatch):
    rig = zeroed_rig(tmp_path)
    try:
        rig.wait(rig.optical_zero(), timeout=60)
        rig.create_profile("cab", "sim")
        for name, pan, tilt in (("dash", 18, -14), ("roof", 0, 45)):
            rig.wait(rig.goto(pan, tilt))
            rig.wait(rig.save_position("cab", name))

        original = study_mod.StudyRun._camera_setup

        def setup_then_slip(self, cam):        # after the zero step: the head loses 0.4 deg
            original(self, cam)
            slip(rig, 0.4)

        monkeypatch.setattr(study_mod.StudyRun, "_camera_setup", setup_then_slip)
        m, sid = rig.start_study("cab", "dark", sweep=False, check=False)
        rig.wait(m, timeout=120)
        deadline = time.monotonic() + 60
        while rig._runs[sid].analysing.is_set():
            assert time.monotonic() < deadline
            time.sleep(0.05)
        st = rig.study(sid)
        assert st["zero"]["offset_deg"] < 0.05
        assert st["drift"]["offset_deg"] == pytest.approx(0.4, abs=0.04)
        # 0.4 deg of pan: 0.4 in the image at tilt -14; at the roof (tilt 45) it is partly a
        # roll about the optical axis, which a shift does not see, and reads ~0.24. Flagged both.
        assert all(p["aim"]["flag"] and p["aim"]["total_deg"] > 0.2 for p in st["positions"])
        flags = " ".join(st["flags"])
        assert "every angle in this study is suspect" in flags and "replayed" in flags
    finally:
        rig.close()


def test_study_without_the_fiducial_flags_the_zero(tmp_path):
    rig = RigController.from_config("sim", tmp_path, camera="sim", time_scale=0.0)
    rig.mount.cfg = replace(rig.mount.cfg, settle_ms=0)
    try:
        rig.enable()
        rig.wait(rig.nudge("left", 14))           # 20 deg from the fiducial
        rig.wait(rig.set_zero())
        rig.create_profile("cab", "sim")
        rig.wait(rig.save_position("cab", "here"))
        m, sid = rig.start_study("cab", "dark", sweep=False, check=False)
        rig.wait(m, timeout=120)
        while rig._runs[sid].analysing.is_set():
            time.sleep(0.05)
        st = rig.study(sid)
        steps = {s["key"]: s["state"] for s in st["steps"]}
        assert steps["zero"] == "failed" and steps["drift"] == "skipped"
        assert any("optical zero failed" in f for f in st["flags"])
    finally:
        rig.close()
