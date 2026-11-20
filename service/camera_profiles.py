"""Camera profiles: one body + lens + aperture, and the constants that belong to it (§4.5).

`k` is valid only for one body, lens and aperture, and the lens constants only for one lens at
one focal length, so they travel together. Changing the lens means a new profile (copy the old
one, then calibrate). A study records the profile it used, frozen, with its content hash.

Stored as TOML under `<data>/profiles/camera/<slug>.toml`. `config/camera.toml` and
`config/analysis.toml` are the template the first profile is made from; after that the
profile, not those files, supplies these values for studies and calibrations:

    lens        focal_mm, distortion_k1, vignetting_map
    exposure    aperture, iso, shutter (the bracket), shutter_model
    calibration k (with the date it was measured and its history), spectral factors
    body_serial, lens_name: what the camera must report; empty = not checked

`k` comes from the target and a meter on the camera page, or is typed in with a note on where
it came from (a lab calibration, another rig), since a luminance meter is not always at hand.
Either way the newest value replaces the profile's `k` and lands in the history with its
source, so a result can always say whether its `k` was measured or typed.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import tomllib
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hal.camera_config import CameraConfig, ExposureConfig, load_camera_config, parse_shutter

from .positions import ProfileError, ProfileNotFound, check_name, now_iso, slug

SCHEMA = 1
HISTORY_KEEP = 50


K_SOURCES = ("target", "typed")


@dataclass(frozen=True)
class KRecord:
    at: str
    k: float
    target_cd_m2: float | None = None                       # target only
    roi: tuple[float, float, float, float] | None = None    # target only
    cv: float | None = None                                 # target only
    drift: float | None = None     # against the k it replaced
    calibration: str = ""          # the calibration folder it came from (target)
    source: str = "target"         # target: measured on the camera page; typed: entered by hand
    note: str = ""                 # typed: where the value comes from


@dataclass(frozen=True)
class CameraProfile:
    name: str
    notes: str = ""
    body_serial: str = ""
    lens_name: str = ""
    focal_mm: float = 18.0
    distortion_k1: float = 0.0
    vignetting_map: str = ""
    aperture: float = 8.0
    iso: int = 100
    shutter: tuple[str, ...] = ()
    shutter_model: str = "apex"
    k: float = 0.0
    k_measured_at: str = ""
    spectral_factor: float = 1.0
    spectral_factors: dict[str, float] = field(default_factory=dict)
    k_history: tuple[KRecord, ...] = ()
    created_at: str = ""
    updated_at: str = ""

    # -- use ----------------------------------------------------------------- #

    def analysis_overrides(self) -> dict[str, Any]:
        """What this profile sets in the analysis config (pipeline.study.config)."""
        out: dict[str, Any] = {"lens.focal_mm": self.focal_mm,
                               "lens.distortion_k1": self.distortion_k1,
                               "lens.vignetting_map": self.vignetting_map,
                               "calibration.spectral_factor": self.spectral_factor,
                               "calibration.spectral_factors": dict(self.spectral_factors)}
        if self.lens_name:
            out["lens.name"] = self.lens_name
        if self.k > 0:
            out["calibration.k"] = self.k
        return out

    def camera_config(self, base: CameraConfig) -> CameraConfig:
        exp = ExposureConfig(aperture=self.aperture, iso=self.iso, shutter=tuple(self.shutter),
                             shutter_model=self.shutter_model,
                             shutter_correction=dict(base.exposure.shutter_correction))
        exp.validate()
        return replace(base, exposure=exp)

    def body_check(self, info: dict[str, Any]) -> list[str]:
        """Mismatches between what the camera reports and this profile; [] when fine."""
        out = []
        serial, lens = str(info.get("serial") or ""), str(info.get("lens") or "")
        if self.body_serial and serial and serial != self.body_serial:
            out.append(f"camera body {serial} is not the profile's {self.body_serial}")
        if self.lens_name and lens and self.lens_name.lower() not in lens.lower() \
                and lens.lower() not in self.lens_name.lower():
            out.append(f"lens reports {lens!r}, the profile is for {self.lens_name!r}")
        return out

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.to_toml().encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["shutter"] = list(self.shutter)
        d["content_hash"] = self.content_hash
        d["k_source"] = self.k_source
        d["k_note"] = self.k_note
        return d

    # -- edits --------------------------------------------------------------- #

    EDITABLE = ("notes", "body_serial", "lens_name", "focal_mm", "distortion_k1",
                "vignetting_map", "aperture", "iso", "shutter", "shutter_model",
                "spectral_factor", "spectral_factors")

    def edited(self, changes: dict[str, Any]) -> "CameraProfile":
        unknown = set(changes) - set(self.EDITABLE)
        if unknown:
            raise ProfileError(f"cannot edit {sorted(unknown)} (k changes only by calibration)")
        c = dict(changes)
        if "shutter" in c:
            c["shutter"] = tuple(str(s) for s in c["shutter"])
        if "spectral_factors" in c:
            c["spectral_factors"] = {str(k): float(v) for k, v in c["spectral_factors"].items()}
        for key in ("focal_mm", "distortion_k1", "aperture", "spectral_factor"):
            if key in c:
                c[key] = float(c[key])
        if "iso" in c:
            c["iso"] = int(c["iso"])
        p = replace(self, **c, updated_at=now_iso())
        p.validate()
        return p

    def with_k(self, rec: KRecord) -> "CameraProfile":
        hist = (self.k_history + (rec,))[-HISTORY_KEEP:]
        return replace(self, k=rec.k, k_measured_at=rec.at, k_history=hist, updated_at=now_iso())

    def with_typed_k(self, k: float, note: str) -> "CameraProfile":
        """Replace k with a value typed by hand. `note` says where it comes from; it goes
        into the history and onto every study that uses it."""
        k, note = float(k), " ".join(str(note).split())
        if not (math.isfinite(k) and 0 < k < 10):
            raise ProfileError("k must be a positive number (cd/m2 per DN N2 / (t ISO), ~0.006)")
        if not note:
            raise ProfileError("say where the typed k comes from (lab calibration, date, ...)")
        drift = (k - self.k) / self.k if self.k > 0 else None
        return self.with_k(KRecord(at=now_iso(), k=k, drift=drift, source="typed",
                                   note=note[:300]))

    @property
    def k_source(self) -> str:
        """Where the current k comes from: target, typed, or config (never recorded)."""
        if not self.k:
            return ""
        last = self.k_history[-1] if self.k_history else None
        return last.source if last and last.k == self.k else "config"

    @property
    def k_note(self) -> str:
        return self.k_history[-1].note if self.k_source == "typed" else ""

    def validate(self) -> None:
        if not 4.0 <= self.focal_mm <= 1000.0:
            raise ProfileError("focal length must be 4 to 1000 mm")
        if not 0.9 <= self.aperture <= 64:
            raise ProfileError("aperture must be an f-number, 0.9 to 64")
        if not 50 <= self.iso <= 25600:
            raise ProfileError("ISO must be 50 to 25600")
        if not self.shutter:
            raise ProfileError("the bracket needs at least one shutter speed")
        try:
            ExposureConfig(self.aperture, self.iso, tuple(self.shutter),
                           self.shutter_model).validate()
        except ValueError as exc:
            raise ProfileError(str(exc)) from None
        for name, v in [("spectral_factor", self.spectral_factor), *self.spectral_factors.items()]:
            if not 0.2 <= v <= 5.0:
                raise ProfileError(f"spectral factor {name} = {v} is outside 0.2..5")

    # -- TOML ---------------------------------------------------------------- #

    def to_toml(self) -> str:
        s, f = _s, _f
        lines = [f"schema = {SCHEMA}", f"name = {s(self.name)}", f"notes = {s(self.notes)}",
                 f"body_serial = {s(self.body_serial)}   # empty: any body",
                 f"lens_name = {s(self.lens_name)}", f"created_at = {s(self.created_at)}",
                 f"updated_at = {s(self.updated_at)}", "",
                 "[lens]", f"focal_mm = {f(self.focal_mm)}",
                 f"distortion_k1 = {f(self.distortion_k1)}",
                 f"vignetting_map = {s(self.vignetting_map)}", "",
                 "[exposure]", f"aperture = {f(self.aperture)}", f"iso = {self.iso}",
                 "shutter = [" + ", ".join(s(x) for x in self.shutter) + "]",
                 f"shutter_model = {s(self.shutter_model)}", "",
                 "[calibration]", f"k = {f(self.k)}", f"k_measured_at = {s(self.k_measured_at)}",
                 f"spectral_factor = {f(self.spectral_factor)}", "",
                 "[calibration.spectral_factors]"]
        lines += [f"{s(k)} = {f(v)}" for k, v in sorted(self.spectral_factors.items())]
        for r in self.k_history:
            lines += ["", "[[k_history]]", f"at = {s(r.at)}", f"k = {f(r.k)}",
                      f"source = {s(r.source)}"]
            if r.note:
                lines.append(f"note = {s(r.note)}")
            if r.target_cd_m2 is not None:
                lines.append(f"target_cd_m2 = {f(r.target_cd_m2)}")
            if r.roi is not None:
                lines.append("roi = [" + ", ".join(f(x) for x in r.roi) + "]")
            if r.cv is not None:
                lines.append(f"cv = {f(r.cv)}")
            if r.calibration:
                lines.append(f"calibration = {s(r.calibration)}")
            if r.drift is not None:
                lines.append(f"drift = {f(r.drift)}")
        return "\n".join(lines) + "\n"

    @classmethod
    def from_toml(cls, text: str) -> "CameraProfile":
        raw = tomllib.loads(text)
        if raw.get("schema", SCHEMA) != SCHEMA:
            raise ProfileError(f"unknown camera profile schema {raw.get('schema')!r}")
        lens, exp, cal = raw.get("lens", {}), raw.get("exposure", {}), raw.get("calibration", {})
        return cls(
            name=str(raw["name"]), notes=str(raw.get("notes", "")),
            body_serial=str(raw.get("body_serial", "")), lens_name=str(raw.get("lens_name", "")),
            focal_mm=float(lens.get("focal_mm", 18.0)),
            distortion_k1=float(lens.get("distortion_k1", 0.0)),
            vignetting_map=str(lens.get("vignetting_map", "")),
            aperture=float(exp.get("aperture", 8.0)), iso=int(exp.get("iso", 100)),
            shutter=tuple(str(x) for x in exp.get("shutter", ())),
            shutter_model=str(exp.get("shutter_model", "apex")),
            k=float(cal.get("k", 0.0)), k_measured_at=str(cal.get("k_measured_at", "")),
            spectral_factor=float(cal.get("spectral_factor", 1.0)),
            spectral_factors={str(k): float(v) for k, v in cal.get("spectral_factors", {}).items()},
            k_history=tuple(_krecord(r) for r in raw.get("k_history", [])),
            created_at=str(raw.get("created_at", "")), updated_at=str(raw.get("updated_at", "")),
        )


def _opt(r: dict[str, Any], key: str) -> float | None:
    return None if r.get(key) is None else float(r[key])


def _krecord(r: dict[str, Any]) -> KRecord:
    source = str(r.get("source", "target"))
    if source not in K_SOURCES:
        raise ProfileError(f"unknown k source {source!r}")
    return KRecord(at=str(r["at"]), k=float(r["k"]), target_cd_m2=_opt(r, "target_cd_m2"),
                   roi=None if r.get("roi") is None else tuple(float(x) for x in r["roi"]),
                   cv=_opt(r, "cv"), drift=_opt(r, "drift"),
                   calibration=str(r.get("calibration", "")), source=source,
                   note=str(r.get("note", "")))


def from_config() -> CameraProfile:
    """The first profile, from config/camera.toml and config/analysis.toml."""
    from photometry.profile import load_analysis_config

    cam, ana = load_camera_config(), load_analysis_config()
    e = cam.exposure
    stamp = now_iso()
    return CameraProfile(
        name="default", notes=f"made from config/camera.toml and config/analysis.toml "
                              f"({ana.lens.name}); body and lens not set, so not checked",
        focal_mm=ana.lens.focal_mm, distortion_k1=ana.lens.distortion_k1,
        vignetting_map=ana.lens.vignetting_map, aperture=e.aperture, iso=e.iso,
        shutter=tuple(e.shutter), shutter_model=e.shutter_model, k=ana.calibration.k,
        spectral_factor=ana.calibration.spectral_factor,
        spectral_factors=dict(ana.calibration.spectral_factors),
        created_at=stamp, updated_at=stamp)


def _s(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def _f(value: float) -> str:
    if not math.isfinite(value):
        raise ProfileError(f"{value} is not finite")
    return repr(float(value))


class CameraProfileStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _path(self, name: str) -> Path:
        return self.root / f"{slug(name)}.toml"

    def names(self) -> list[str]:
        if not self.root.is_dir():
            return []
        out = []
        for p in sorted(self.root.glob("*.toml")):
            try:
                out.append(CameraProfile.from_toml(p.read_text(encoding="utf-8")).name)
            except (tomllib.TOMLDecodeError, KeyError, ValueError):
                continue
        return out

    def ensure_default(self) -> None:
        if not self.names():
            self.save(from_config())

    def load(self, name: str) -> CameraProfile:
        p = self._path(name)
        if not p.is_file():
            raise ProfileNotFound(f"no camera profile {name!r}")
        return CameraProfile.from_toml(p.read_text(encoding="utf-8"))

    def save(self, profile: CameraProfile) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(profile.name)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(profile.to_toml(), encoding="utf-8")
        os.replace(tmp, path)

    def create(self, name: str, copy_from: str | None = None) -> CameraProfile:
        """A new profile, copied from another (the usual case: a new lens on the same body).
        The copy starts uncalibrated: its k must be measured with the new lens."""
        name = check_name(name, "camera profile")
        if self._path(name).exists():
            raise ProfileError(f"a camera profile named like {name!r} already exists")
        base = self.load(copy_from) if copy_from else from_config()
        stamp = now_iso()
        p = replace(base, name=name, k=0.0, k_measured_at="", k_history=(),
                    notes=f"copied from {copy_from}" if copy_from else base.notes,
                    created_at=stamp, updated_at=stamp)
        self.save(p)
        return p

    def delete(self, name: str) -> str:
        path = self._path(name)
        if not path.is_file():
            raise ProfileNotFound(f"no camera profile {name!r}")
        trash = self.root / ".trash"
        trash.mkdir(exist_ok=True)
        dest = trash / f"{path.stem}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.toml"
        os.replace(path, dest)
        return str(dest)


def parse_bracket(text: str) -> tuple[str, ...]:
    """'1/4000, 1/1000, 4' -> labels, refusing anything that is not a timed speed."""
    out = tuple(x.strip() for x in text.replace(";", ",").split(",") if x.strip())
    for x in out:
        try:
            parse_shutter(x)
        except ValueError:
            raise ProfileError(f"{x!r} is not a shutter speed") from None
    return out
