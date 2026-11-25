"""Live-view shutter control against a body that shows frames at the set exposure, late."""

from __future__ import annotations

import io
import sys
import types
from collections import deque
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_camera_gphoto2_mock import _camera, make_fake_gp  # noqa: E402

from hal.camera_config import LiveViewConfig, load_camera_config, parse_shutter  # noqa: E402
from hal.live_exposure import CLIPPED, LiveExposure, error_ev  # noqa: E402

THIRDS = (["bulb"] + [f"{v:g}" for v in (30, 15, 8, 4, 3.2, 2.5, 2, 1.6, 1.3, 1, 0.8, 0.6, 0.5,
                                         0.4, 0.3)]
          + [f"1/{v}" for v in (4, 5, 6, 8, 10, 13, 15, 20, 25, 30, 40, 50, 60, 80, 100, 125,
                                160, 200, 250, 320, 400, 500, 640, 800, 1000, 1250, 1600, 2000,
                                2500, 3200, 4000)])
CFG = load_camera_config().live_view


def jpeg(linear: np.ndarray, slope: float = 1.0) -> bytes:
    """`linear` 0..1 -> gamma-coded JPEG; `slope` > 1 is a contrastier tone curve."""
    g = np.clip(linear, 0, 1) ** (slope / 2.2)
    buf = io.BytesIO()
    Image.fromarray((g * 255).astype(np.uint8)).convert("RGB").save(buf, "JPEG", quality=90)
    return buf.getvalue()


def uniform(v: float) -> np.ndarray:
    return np.full((120, 180), v)


def cab(panel: float = 0.1) -> np.ndarray:
    s = uniform(1.0)
    s[:, : int(180 * panel)] = 400.0      # a lit panel far above the trim
    return s


def level(frame: bytes) -> float:
    return float(np.asarray(Image.open(io.BytesIO(frame)).convert("L")).mean())


def clipped(frame: bytes) -> float:
    return float((np.asarray(Image.open(io.BytesIO(frame)).convert("L")) >= CLIPPED).mean())


def run(scene: np.ndarray, start: str, cfg: LiveViewConfig = CFG, slope: float = 1.0,
        frames: int = 80, latency: int = 2):
    loop = LiveExposure(cfg)
    loop.label = start
    loop.start(THIRDS)
    shown = deque([start] * (latency + 1), maxlen=latency + 1)
    changes = []
    for _ in range(frames):
        frame = jpeg(scene * parse_shutter(shown[0]), slope)
        new = loop.update(frame, THIRDS)
        if new:
            loop.changed(new)
            changes.append(new)
        shown.append(loop.label)
    return loop, frame, changes


@pytest.mark.parametrize("slope", [1.0, 1.5])
@pytest.mark.parametrize("start", ["4", "1/60", "1/4000"])
@pytest.mark.parametrize("scale", [0.1, 5.0, 300.0])
def test_settles_from_either_side_without_hunting(scale, start, slope):
    loop, frame, changes = run(uniform(scale), start, slope=slope)
    assert loop.settled and loop.at_limit is None
    assert 70 < level(frame) < 150
    assert len(changes) <= 8


def test_the_four_seconds_a_bracket_leaves_behind_are_undone():
    assert level(jpeg(uniform(5.0) * 4)) == 255
    loop, frame, _ = run(uniform(5.0), "4")
    assert loop.settled and clipped(frame) == 0 and 70 < level(frame) < 150


def test_lit_panel_is_not_burnt_out_for_the_trim():
    loop, frame, _ = run(cab(0.10), "4")
    assert loop.settled and clipped(frame) < CFG.highlight_fraction
    loop, frame, _ = run(cab(0.02), "4")                 # small sources may clip
    assert loop.settled and level(frame) > 60


def test_limits_are_reported_not_exceeded():
    loop, _, _ = run(uniform(1e6), "1/60")
    assert loop.label == "1/4000" and loop.at_limit == "bright"
    loop, _, _ = run(uniform(1e-4), "1/60")
    assert loop.label == "4" and loop.at_limit == "dark"  # 30 s and bulb are not used


def test_broken_frame_changes_nothing():
    assert error_ev(b"\xff\xd8liveview", CFG) is None
    loop = LiveExposure(replace(CFG, settle_frames=0))
    assert loop.update(b"nope", THIRDS) is None and loop.settled


@pytest.mark.parametrize("bad", [
    {"shutter_start": "8"}, {"target_level": 0}, {"highlight_fraction": 1.0},
    {"deadband_ev": 0.1}, {"settle_frames": -1}])
def test_config_is_checked(bad):
    with pytest.raises(ValueError):
        replace(CFG, **bad).validate()


# -- through Gphoto2Camera ------------------------------------------------- #


def rig(scene: np.ndarray, latency: int = 2):
    gp, settings, log = make_fake_gp(busy_on_first_capture=False)
    settings["shutterspeed"] = ("4", THIRDS, False)       # what a bracket leaves behind
    shown: deque[str] = deque(maxlen=latency + 1)

    def capture_preview(self):
        settings["viewfinder"] = (1, None, False)
        shown.append(settings["shutterspeed"][0])
        log.append("preview")
        data = jpeg(scene * parse_shutter(shown[0]))
        return types.SimpleNamespace(get_data_and_size=lambda: data)

    gp.Camera.capture_preview = capture_preview
    mod, cam = _camera(gp)
    cam.connect()
    return mod, cam, gp, settings, log


def test_first_frame_is_already_exposed():
    _, cam, _, settings, log = rig(uniform(300.0))
    assert 60 < level(cam.preview()) < 160
    assert log.index("set shutterspeed=1/60") < log.index("preview")   # before the mirror goes up
    assert cam.live_exposure()["auto"] and cam.live_exposure()["at_limit"] is None
    assert settings["iso"][0] == "100" and settings["aperture"][0] == "8"   # k's settings


def test_bracket_shutters_are_untouched_and_live_view_recovers(tmp_path):
    _, cam, _, settings, log = rig(uniform(5.0))
    cam.preview()
    was = cam.live_exposure()["shutter"]
    m = cam.capture_bracket(tmp_path)
    assert [f.shutter_label for f in m.frames] == list(cam.config.exposure.shutter)
    assert settings["shutterspeed"][0] == "4"
    n = len(log)
    assert 60 < level(cam.preview()) < 160
    assert log[n] == f"set shutterspeed={was}"


def test_refusing_body_still_streams(caplog):
    mod, cam, gp, _, _ = rig(uniform(5.0))
    ok = gp.Camera.set_single_config

    def refuse(self, name, w):
        if name == "shutterspeed":
            raise gp.GPhoto2Error(-2)
        ok(self, name, w)

    gp.Camera.set_single_config = refuse
    for _ in range(12):
        assert cam.preview()[:2] == b"\xff\xd8"
    assert cam.live_exposure()["auto"] is False
    assert sum("not set" in r.message for r in caplog.records) == cam.LIVE_REFUSALS


def test_lost_link_still_raises():
    mod, cam, gp, _, _ = rig(uniform(5.0))

    def lost(self, name, w):
        raise gp.GPhoto2Error(gp.GP_ERROR_IO)

    gp.Camera.set_single_config = lost
    with pytest.raises(mod.CameraDisconnected):
        cam.preview()


def test_can_be_switched_off():
    _, cam, _, settings, _ = rig(uniform(5.0))
    cam.apply_config(replace(cam.config, live_view=replace(CFG, auto_exposure=False)))
    cam.preview()
    assert settings["shutterspeed"][0] == "4"
