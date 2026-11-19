"""Gear ratio entry in config/mount.toml: tooth counts or a plain ratio, not both."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from hal.config import DEFAULT_CONFIG, load_config

TEETH = re.compile(r"^gear_teeth_(driver|driven) = .*\n", re.M)


def _tilt_variant(tmp_path: Path, tilt_gear: str) -> Path:
    """Copy of the shipped config with the tilt axis gearing replaced."""
    text = DEFAULT_CONFIG.read_text(encoding="utf-8")
    head, tilt = text.split("[axis.tilt]")
    tilt = TEETH.sub("", tilt)
    path = tmp_path / "mount.toml"
    path.write_text(f"{head}[axis.tilt]\n{tilt_gear}\n{tilt}", encoding="utf-8")
    return path


def test_shipped_config_uses_teeth():
    cfg = load_config()
    assert cfg.tilt.gear_ratio == pytest.approx(64 / 21)
    assert cfg.tilt.gear_label == "64:21"
    assert cfg.pan.gear_ratio == pytest.approx(144 / 17)


def test_plain_ratio(tmp_path):
    cfg = load_config(_tilt_variant(tmp_path, "gear_ratio = 3.05"))
    assert cfg.tilt.gear_ratio == 3.05
    assert cfg.tilt.steps_per_deg == pytest.approx(200 * 16 * 3.05 / 360)


@pytest.mark.parametrize("gear", [
    "gear_ratio = 3.05\ngear_teeth_driver = 21\ngear_teeth_driven = 64",  # both
    "gear_teeth_driver = 21",                                             # half a pair
    "",                                                                   # neither
    "gear_ratio = 0",
    "gear_ratio = -3.0",
    "gear_teeth_driver = 0\ngear_teeth_driven = 64",
])
def test_bad_gearing_refused(tmp_path, gear):
    with pytest.raises(ValueError, match="tilt"):
        load_config(_tilt_variant(tmp_path, gear))
