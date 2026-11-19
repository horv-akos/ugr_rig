"""Mount configuration.

Every number that describes the physical head lives in one TOML file, not in
the code. Several of them (settle time, backlash, soft limits)
are placeholders until they are measured on the built head - see
docs/PIVOT_DETAIL.md §8.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config" / "mount.toml"


@dataclass(frozen=True)
class Pins:
    pan_step: int
    pan_dir: int
    tilt_step: int
    tilt_dir: int
    enable: int
    enable_active_low: bool = True

    def __post_init__(self) -> None:
        used = [self.pan_step, self.pan_dir, self.tilt_step, self.tilt_dir, self.enable]
        if len(set(used)) != len(used):
            raise ValueError("pin map assigns the same GPIO twice")
        clash = sorted(set(used) & {18, 19})
        if clash:
            raise ValueError(
                f"GPIO {clash} is used by pigpio's timing peripheral - pick another pin"
            )


@dataclass(frozen=True)
class AxisConfig:
    name: str
    gear_ratio: float  # motor turns per output turn
    gear_label: str  # how the ratio was given, e.g. "64:21" or "3.047619"
    min_deg: float
    max_deg: float
    max_speed_deg_s: float
    accel_deg_s2: float
    start_speed_deg_s: float
    backlash_deg: float
    approach_sign: int  # +1: always arrive moving towards increasing degrees
    invert: bool
    motor_steps_per_rev: int
    microsteps: int

    @property
    def steps_per_deg(self) -> float:
        return self.motor_steps_per_rev * self.microsteps * self.gear_ratio / 360.0

    @property
    def deg_per_step(self) -> float:
        return 1.0 / self.steps_per_deg

    def deg_to_steps(self, deg: float) -> int:
        return int(round(deg * self.steps_per_deg))

    def steps_to_deg(self, steps: int) -> float:
        return steps / self.steps_per_deg

    def validate(self) -> None:
        if not self.gear_ratio > 0:
            raise ValueError(f"{self.name}: gear ratio must be positive")
        if self.min_deg >= self.max_deg:
            raise ValueError(f"{self.name}: min_deg must be below max_deg")
        if self.max_speed_deg_s <= 0:
            raise ValueError(f"{self.name}: max_speed_deg_s must be positive")
        if self.start_speed_deg_s <= 0:
            raise ValueError(f"{self.name}: start_speed_deg_s must be positive")
        if self.approach_sign not in (-1, 1):
            raise ValueError(f"{self.name}: approach_sign must be +1 or -1")
        if self.backlash_deg < 0:
            raise ValueError(f"{self.name}: backlash_deg cannot be negative")


@dataclass(frozen=True)
class MountConfig:
    pins: Pins
    pan: AxisConfig
    tilt: AxisConfig
    settle_ms: int
    pulse_width_us: int
    enable_settle_ms: int
    dir_setup_us: int
    source: Path | None = None

    def axis(self, name: str) -> AxisConfig:
        if name == "pan":
            return self.pan
        if name == "tilt":
            return self.tilt
        raise ValueError(f"no such axis: {name!r}")


def _gear_ratio(name: str, section: dict[str, Any]) -> tuple[float, str]:
    """Ratio from either `gear_ratio` or the `gear_teeth_driver/driven` pair.

    A plain number lets a value measured with `mountctl ratio` go straight in;
    tooth counts document which printed gears are fitted. Both at once is
    refused, so there is never a question of which one the head is using.
    """
    has_ratio = "gear_ratio" in section
    teeth = [k for k in ("gear_teeth_driver", "gear_teeth_driven") if k in section]
    if has_ratio and teeth:
        raise ValueError(
            f"{name}: set gear_ratio or gear_teeth_driver/gear_teeth_driven, not both"
        )
    if has_ratio:
        ratio = float(section["gear_ratio"])
        return ratio, f"{ratio:g}"
    if len(teeth) != 2:
        raise ValueError(
            f"{name}: needs gear_ratio, or both gear_teeth_driver and gear_teeth_driven"
        )
    driver = int(section["gear_teeth_driver"])
    driven = int(section["gear_teeth_driven"])
    if driver <= 0 or driven <= 0:
        raise ValueError(f"{name}: gear tooth counts must be positive")
    return driven / driver, f"{driven}:{driver}"


def load_config(path: str | Path | None = None) -> MountConfig:
    path = Path(path) if path else DEFAULT_CONFIG
    with open(path, "rb") as fh:
        raw: dict[str, Any] = tomllib.load(fh)

    mount = raw.get("mount", {})
    microsteps = int(mount.get("microsteps", 16))
    motor_steps = int(mount.get("motor_steps_per_rev", 200))

    def axis(name: str) -> AxisConfig:
        section = raw["axis"][name]
        ratio, label = _gear_ratio(name, section)
        cfg = AxisConfig(
            name=name,
            gear_ratio=ratio,
            gear_label=label,
            min_deg=float(section["min_deg"]),
            max_deg=float(section["max_deg"]),
            max_speed_deg_s=float(section["max_speed_deg_s"]),
            accel_deg_s2=float(section["accel_deg_s2"]),
            start_speed_deg_s=float(section.get("start_speed_deg_s", 0.25)),
            backlash_deg=float(section.get("backlash_deg", 0.0)),
            approach_sign=int(section.get("approach_sign", 1)),
            invert=bool(section.get("invert", False)),
            motor_steps_per_rev=motor_steps,
            microsteps=microsteps,
        )
        cfg.validate()
        return cfg

    pins = Pins(**raw["pins"])
    return MountConfig(
        pins=pins,
        pan=axis("pan"),
        tilt=axis("tilt"),
        settle_ms=int(mount.get("settle_ms", 800)),
        pulse_width_us=int(mount.get("pulse_width_us", 5)),
        enable_settle_ms=int(mount.get("enable_settle_ms", 50)),
        dir_setup_us=int(mount.get("dir_setup_us", 100)),
        source=path,
    )
