"""Camera configuration and shutter-speed arithmetic.

config/camera.toml -> dataclasses. The shutter helpers live here because both
the real camera and the simulator need the same answer to "what exposure time
did this frame actually get".
"""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CAMERA_CONFIG = Path(__file__).resolve().parents[1] / "config" / "camera.toml"


# --------------------------------------------------------------------------- #
# shutter speeds
# --------------------------------------------------------------------------- #


def parse_shutter(label: str) -> float:
    """'1/4000' -> 0.00025, '0.3' -> 0.3, '4' -> 4.0, '2"' -> 2.0.

    Raises ValueError for 'bulb' and anything else that is not a timed speed.
    """
    s = str(label).strip().rstrip('"').rstrip("s").strip()
    if "/" in s:
        num, den = s.split("/", 1)
        value = float(num) / float(den)
    else:
        value = float(s)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"not a shutter speed: {label!r}")
    return value


def apex_time(marked_s: float) -> float:
    """Marked shutter speed -> the 1/3-stop power of two it stands for.

    Cameras time the shutter in APEX steps; the labels are rounded for humans.
    '1/60' is Tv 6 = 1/64 s, '1/125' is Tv 7 = 1/128 s, '0.3' is Tv 5/3 = 0.315 s.
    """
    tv = -math.log2(marked_s)
    return 2.0 ** (-round(tv * 3.0) / 3.0)


def effective_time(label: str, model: str = "apex",
                   correction: dict[str, float] | None = None) -> float:
    marked = parse_shutter(label)
    if model == "apex":
        t = apex_time(marked)
    elif model == "nominal":
        t = marked
    else:
        raise ValueError(f"unknown shutter_model {model!r}")
    if correction and label in correction:
        t *= float(correction[label])
    return t


def nearest_choice(target_s: float, choices: list[str]) -> str:
    """Pick the camera's choice closest to target_s in log space (i.e. in stops)."""
    best, best_err = None, math.inf
    for c in choices:
        try:
            v = parse_shutter(c)
        except ValueError:
            continue
        err = abs(math.log2(v / target_s))
        if err < best_err:
            best, best_err = c, err
    if best is None:
        raise ValueError("camera offered no timed shutter speeds")
    if best_err > 1 / 6 + 1e-9:
        raise ValueError(
            f"no shutter speed within 1/6 stop of {target_s:g} s (closest {best!r})"
        )
    return best


def parse_aperture(label: str) -> float:
    s = str(label).strip().lower()
    if s.startswith("f/"):
        s = s[2:]
    return float(s)


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ExposureConfig:
    aperture: float
    iso: int
    shutter: tuple[str, ...]
    shutter_model: str = "apex"
    shutter_correction: dict[str, float] = field(default_factory=dict)

    def effective_times(self) -> list[float]:
        return [effective_time(s, self.shutter_model, self.shutter_correction)
                for s in self.shutter]

    def validate(self) -> None:
        if not self.shutter:
            raise ValueError("exposure.shutter is empty")
        times = [parse_shutter(s) for s in self.shutter]
        if len(set(times)) != len(times):
            raise ValueError("exposure.shutter lists the same speed twice")
        if max(times) > 30:
            raise ValueError("longest timed speed on the 2000D is 30 s; bulb is not supported")
        if self.shutter_model not in ("apex", "nominal"):
            raise ValueError("exposure.shutter_model must be 'apex' or 'nominal'")


@dataclass(frozen=True)
class LiveViewConfig:
    """Shutter control for live view (hal/live_exposure.py). No bracket frame uses it."""

    auto_exposure: bool = True
    shutter_start: str = "1/60"
    shutter_fastest: str = "1/4000"
    shutter_slowest: str = "4"
    target_level: int = 110
    highlight_fraction: float = 0.05
    deadband_ev: float = 0.5
    settle_frames: int = 3

    def validate(self) -> None:
        if not (parse_shutter(self.shutter_fastest) <= parse_shutter(self.shutter_start)
                <= parse_shutter(self.shutter_slowest)):
            raise ValueError("live_view: shutter_start must lie between fastest and slowest")
        if not 1 <= self.target_level <= 254:
            raise ValueError("live_view.target_level must be 1..254")
        if not 0 <= self.highlight_fraction < 1:
            raise ValueError("live_view.highlight_fraction must be 0..1")
        if self.deadband_ev < 1 / 3:
            raise ValueError("live_view.deadband_ev must be at least 1/3, the shutter's step")
        if self.settle_frames < 0:
            raise ValueError("live_view.settle_frames must not be negative")


@dataclass(frozen=True)
class CameraConfig:
    model: str
    capture_target: str
    delete_after_verify: bool
    image_format: str
    white_balance: str
    download_timeout_s: float
    min_file_bytes: int
    exif_check: bool
    exposure: ExposureConfig
    live_view: LiveViewConfig = field(default_factory=LiveViewConfig)
    source: Path | None = None

    def snapshot(self) -> dict[str, Any]:
        e = self.exposure
        return {
            "model": self.model,
            "capture_target": self.capture_target,
            "aperture": e.aperture,
            "iso": e.iso,
            "shutter": list(e.shutter),
            "shutter_model": e.shutter_model,
            "shutter_correction": dict(e.shutter_correction),
        }


def load_camera_config(path: str | Path | None = None) -> CameraConfig:
    path = Path(path) if path else DEFAULT_CAMERA_CONFIG
    with open(path, "rb") as fh:
        raw: dict[str, Any] = tomllib.load(fh)
    cam = raw.get("camera", {})
    exp = raw["exposure"]
    exposure = ExposureConfig(
        aperture=float(exp["aperture"]),
        iso=int(exp["iso"]),
        shutter=tuple(str(s) for s in exp["shutter"]),
        shutter_model=str(exp.get("shutter_model", "apex")),
        shutter_correction={str(k): float(v)
                            for k, v in exp.get("shutter_correction", {}).items()},
    )
    exposure.validate()
    live_view = LiveViewConfig(**raw.get("live_view", {}))
    live_view.validate()
    target = str(cam.get("capture_target", "card"))
    if target not in ("card", "ram"):
        raise ValueError("camera.capture_target must be 'card' or 'ram'")
    return CameraConfig(
        model=str(cam.get("model", "Canon EOS 2000D")),
        capture_target=target,
        delete_after_verify=bool(cam.get("delete_after_verify", True)),
        image_format=str(cam.get("image_format", "RAW")),
        white_balance=str(cam.get("white_balance", "Daylight")),
        download_timeout_s=float(cam.get("download_timeout_s", 30)),
        min_file_bytes=int(cam.get("min_file_bytes", 5_000_000)),
        exif_check=bool(cam.get("exif_check", True)),
        exposure=exposure,
        live_view=live_view,
        source=path,
    )
