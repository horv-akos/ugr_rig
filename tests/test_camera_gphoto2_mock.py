"""Gphoto2Camera control flow against a fake libgphoto2.

This does not prove anything about the real 2000D - only that the code asks for
the right things in the right order, retries on busy, verifies downloads, and
refuses a body in the wrong mode. `ugrctl camera info` on the real body is the
next check.

    python -m pytest tests/        or        python tests/test_camera_gphoto2_mock.py
"""

from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FRAME_BYTES = 6_000_000
LIVE_JPEG = bytes([0xFF, 0xD8]) + b"liveview"


def make_fake_gp(mode="Manual", busy_on_first_capture=True, truncate=False):
    gp = types.SimpleNamespace(
        GP_CAPTURE_IMAGE=0, GP_FILE_TYPE_NORMAL=1, GP_EVENT_TIMEOUT=0,
        GP_ERROR_CAMERA_BUSY=-110, GP_ERROR_NOT_SUPPORTED=-6, GP_ERROR_MODEL_NOT_FOUND=-105,
        GP_ERROR_IO_USB_FIND=-52, GP_ERROR_IO_USB_CLAIM=-53, GP_ERROR_IO=-7,
        GP_ERROR_TIMEOUT=-10, GP_WIDGET_TEXT=2, GP_WIDGET_RADIO=5, GP_WIDGET_MENU=6,
    )

    class GPhoto2Error(Exception):
        def __init__(self, code):
            super().__init__(f"gphoto2 error {code}")
            self.code = code

    gp.GPhoto2Error = GPhoto2Error
    gp.use_python_logging = lambda **kw: object()

    shutter = ["bulb", "30", "15", "8", "4", "2", "1", "0.5", "1/4", "1/8", "1/15", "1/30",
               "1/60", "1/125", "1/250", "1/500", "1/1000", "1/2000", "1/4000"]
    settings = {
        "shutterspeed": ("1/125", shutter, False),
        "aperture": ("5.6", ["3.5", "4", "5.6", "8", "11", "16", "22"], False),
        "iso": ("Auto", ["Auto", "100", "200", "400"], False),
        "imageformat": ("Large Fine JPEG", ["Large Fine JPEG", "RAW", "RAW + Large Fine JPEG"],
                        False),
        "whitebalance": ("Auto", ["Auto", "Daylight"], False),
        "capturetarget": ("Internal RAM", ["Internal RAM", "Memory card"], False),
        "autoexposuremode": (mode, None, True),
        "focusmode": ("Manual", None, True),
        "cameramodel": ("Canon EOS 2000D", None, True),
        "eosserialnumber": ("123456", None, True),
        "lensname": ("EF-S18-55mm f/3.5-5.6 III", None, True),
        "batterylevel": ("100%", None, True),
        "autopoweroff": ("1", ["0", "1", "2"], False),
        "viewfinder": (0, None, False),                     # toggle: live view on/off
    }

    class Widget:
        def __init__(self, name):
            self.name = name

        def get_value(self):
            return settings[self.name][0]

        def set_value(self, v):
            val, ch, ro = settings[self.name]
            self._pending = v

        def get_choices(self):
            ch = settings[self.name][1]
            if ch is None:
                raise GPhoto2Error(-2)
            return iter(ch)

        def get_type(self):
            return gp.GP_WIDGET_TEXT if settings[self.name][1] is None else gp.GP_WIDGET_RADIO

        def get_readonly(self):
            return settings[self.name][2]

    class Tree:
        def get_child_by_name(self, name):
            if name not in settings:
                raise GPhoto2Error(-2)
            return Widget(name)

    log = []

    class Camera:
        captures = 0

        def init(self):
            log.append("init")

        def exit(self):
            log.append("exit")

        def get_config(self):
            return Tree()

        def get_single_config(self, name):
            if name not in settings:
                raise GPhoto2Error(-2)
            return Widget(name)

        def set_single_config(self, name, w):
            val, ch, ro = settings[name]
            settings[name] = (w._pending, ch, ro)
            log.append(f"set {name}={w._pending}")

        def wait_for_event(self, ms):
            return gp.GP_EVENT_TIMEOUT, None

        def capture(self, kind):
            Camera.captures += 1
            if busy_on_first_capture and Camera.captures == 1:
                raise GPhoto2Error(gp.GP_ERROR_CAMERA_BUSY)
            n = Camera.captures
            log.append(f"capture @ {settings['shutterspeed'][0]}")
            return types.SimpleNamespace(folder="/store_00020001/DCIM/100CANON",
                                         name=f"IMG_{n:04d}.CR2")

        def file_get_info(self, folder, name):
            return types.SimpleNamespace(file=types.SimpleNamespace(size=FRAME_BYTES))

        def file_get(self, folder, name, kind):
            size = FRAME_BYTES - (1000 if truncate else 0)
            return types.SimpleNamespace(save=lambda p: Path(p).write_bytes(b"\0" * size))

        def file_delete(self, folder, name):
            log.append(f"delete {name}")

        def capture_preview(self):
            settings["viewfinder"] = (1, None, False)      # Canon raises the mirror
            log.append("preview")
            return types.SimpleNamespace(get_data_and_size=lambda: LIVE_JPEG)

    gp.Camera = Camera
    return gp, settings, log


def _camera(gp):
    sys.modules["gphoto2"] = gp
    import hal.camera as camera_mod
    from hal.camera_config import load_camera_config

    camera_mod._verify_raw = lambda path: None            # no real CR2 here
    cfg = load_camera_config()
    import dataclasses
    cfg = dataclasses.replace(cfg, exif_check=False)
    return camera_mod, camera_mod.Gphoto2Camera(cfg)


def test_bracket_flow():
    gp, settings, log = make_fake_gp()
    camera_mod, cam = _camera(gp)
    cam.connect()
    assert settings["imageformat"][0] == "RAW"
    assert settings["capturetarget"][0] == "Memory card"
    assert settings["iso"][0] == "100"
    assert settings["aperture"][0] == "8"
    with tempfile.TemporaryDirectory() as d:
        m = cam.capture_bracket(d, meta={"region": "test"})
        assert [f.shutter_label for f in m.frames] == \
            ["1/4000", "1/1000", "1/250", "1/60", "1/15", "1/4", "1", "4"]
        assert m.finished_at
        assert abs(m.frames[3].t_effective_s - 1 / 64) < 1e-12
        assert all((Path(d) / f.file).stat().st_size == FRAME_BYTES for f in m.frames)
        loaded = camera_mod.BracketManifest.load(d)
        assert len(loaded.frames) == 8
    shots = [e for e in log if e.startswith("capture")]
    assert shots[0] == "capture @ 1/4000" and shots[-1] == "capture @ 4"
    assert sum(e.startswith("delete") for e in log) == 8      # deleted only after verify
    cam.close()


def test_live_view_ends_before_the_bracket():
    gp, settings, log = make_fake_gp(busy_on_first_capture=False)
    camera_mod, cam = _camera(gp)
    cam.connect()
    assert cam.preview() == LIVE_JPEG
    assert settings["viewfinder"][0] == 1
    with tempfile.TemporaryDirectory() as d:
        cam.capture_bracket(d)
    first_shot = next(i for i, e in enumerate(log) if e.startswith("capture"))
    assert "set viewfinder=0" in log[:first_shot]      # mirror down before the first frame
    assert settings["viewfinder"][0] == 0
    cam.end_preview()                                 # already off: no second write
    assert log.count("set viewfinder=0") == 1


def test_refuses_wrong_mode():
    gp, _, _ = make_fake_gp(mode="AV")
    camera_mod, cam = _camera(gp)
    try:
        cam.connect()
    except camera_mod.CameraSetupError as exc:
        assert "M" in str(exc)
    else:
        raise AssertionError("connected with the dial on Av")


def _refuse_power_off(gp, code):
    """The 2000D: autopoweroff is a text widget, and writing it fails every time."""
    gp.autopoweroff_writes = 0
    original = gp.Camera.set_single_config

    def set_single_config(self, name, w):
        if name == "autopoweroff":
            gp.autopoweroff_writes += 1
            raise gp.GPhoto2Error(code)
        original(self, name, w)

    gp.Camera.set_single_config = set_single_config


def test_refused_auto_power_off_is_only_a_warning(caplog):
    gp, settings, _ = make_fake_gp()
    settings["autopoweroff"] = ("30", None, False)
    _refuse_power_off(gp, gp.GP_ERROR_CAMERA_BUSY)
    _, cam = _camera(gp)
    cam.connect()
    assert "auto power off" in caplog.text
    assert settings["aperture"][0] == "8"
    tried = gp.autopoweroff_writes
    cam.apply_config(cam.config)              # a camera profile: no second round of waiting
    assert gp.autopoweroff_writes == tried


def test_lost_link_during_auto_power_off_still_raises():
    gp, settings, _ = make_fake_gp()
    settings["autopoweroff"] = ("30", None, False)
    _refuse_power_off(gp, gp.GP_ERROR_IO)
    camera_mod, cam = _camera(gp)
    with pytest.raises(camera_mod.CameraDisconnected):
        cam.connect()


def test_busy_on_a_required_setting_still_raises(monkeypatch):
    gp, _, _ = make_fake_gp()
    camera_mod, cam = _camera(gp)
    monkeypatch.setattr(camera_mod.time, "sleep", lambda s: None)
    original = gp.Camera.set_single_config

    def set_single_config(self, name, w):
        if name == "iso":
            raise gp.GPhoto2Error(gp.GP_ERROR_CAMERA_BUSY)
        original(self, name, w)

    gp.Camera.set_single_config = set_single_config
    with pytest.raises(camera_mod.CameraBusy):
        cam.connect()


def test_truncated_download():
    gp, _, log = make_fake_gp(truncate=True, busy_on_first_capture=False)
    camera_mod, cam = _camera(gp)
    cam.connect()
    with tempfile.TemporaryDirectory() as d:
        try:
            cam.capture_bracket(d)
        except camera_mod.BracketError:
            pass
        else:
            raise AssertionError("truncated file accepted")
        assert not any(e.startswith("delete") for e in log)   # card copy kept


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
