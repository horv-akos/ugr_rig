"""Position profiles: the named directions a study visits, per vehicle configuration.

A position list is only worth anything if it replays identically on the next visit, so a
profile carries what makes that possible: the zero reference (where the fiducial sits in that
cab, how the rig is placed at the eye point) and the forward gaze. Angles are mount degrees
relative to that zero, recorded after the backlash was taken up along `approach_sign`.

Profiles are TOML files under `<data>/profiles/positions/`, so they can be read, diffed and
hashed into results. `tomllib` reads them. The standard library has no TOML writer, and this
schema is too small to justify a dependency, so `to_toml()` writes it directly.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import tomllib
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = 1
log = logging.getLogger(__name__)


class ProfileError(ValueError):
    """A profile request that cannot be honoured (bad name, duplicate, bad order)."""


class ProfileNotFound(ProfileError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9._-]+", "-", name.strip().lower()).strip("-.")
    if not s:
        raise ProfileError(f"name {name!r} has no usable characters")
    return s


def check_name(name: str, what: str) -> str:
    name = name.strip()
    if not name or len(name) > 80:
        raise ProfileError(f"{what} name must be 1 to 80 characters")
    if any(ord(c) < 32 for c in name):
        raise ProfileError(f"{what} name contains control characters")
    return name


@dataclass(frozen=True)
class Position:
    name: str
    pan_deg: float
    tilt_deg: float
    saved_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "pan_deg": self.pan_deg, "tilt_deg": self.tilt_deg,
                "saved_at": self.saved_at}


@dataclass(frozen=True)
class PositionProfile:
    name: str
    vehicle: str = ""
    zero_reference: str = ""            # free text: fiducial location, rig placement
    forward_gaze: tuple[float, float] | None = None   # (pan_deg, tilt_deg), mount coordinates
    positions: tuple[Position, ...] = field(default_factory=tuple)
    created_at: str = ""
    updated_at: str = ""

    # -- queries ------------------------------------------------------------ #

    def position(self, name: str) -> Position:
        for p in self.positions:
            if p.name == name:
                return p
        raise ProfileNotFound(f"profile {self.name!r} has no position {name!r}")

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.to_toml().encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "vehicle": self.vehicle,
            "zero_reference": self.zero_reference,
            "forward_gaze": list(self.forward_gaze) if self.forward_gaze else None,
            "positions": [p.as_dict() for p in self.positions],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "content_hash": self.content_hash,
        }

    # -- edits (each returns a new profile) --------------------------------- #

    def with_position(self, pos: Position) -> "PositionProfile":
        """Replace the position of that name in place, or append it."""
        check_name(pos.name, "position")
        out = list(self.positions)
        for i, p in enumerate(out):
            if p.name == pos.name:
                out[i] = pos
                break
        else:
            out.append(pos)
        return replace(self, positions=tuple(out), updated_at=now_iso())

    def without_position(self, name: str) -> "PositionProfile":
        self.position(name)
        return replace(self, positions=tuple(p for p in self.positions if p.name != name),
                       updated_at=now_iso())

    def reordered(self, names: list[str]) -> "PositionProfile":
        current = [p.name for p in self.positions]
        if sorted(names) != sorted(current):
            raise ProfileError(f"new order must name every position exactly once: {current}")
        by_name = {p.name: p for p in self.positions}
        return replace(self, positions=tuple(by_name[n] for n in names), updated_at=now_iso())

    def with_gaze(self, pan_deg: float, tilt_deg: float) -> "PositionProfile":
        return replace(self, forward_gaze=(pan_deg, tilt_deg), updated_at=now_iso())

    # -- TOML --------------------------------------------------------------- #

    def to_toml(self) -> str:
        lines = [
            f"schema = {SCHEMA}",
            f"name = {_s(self.name)}",
            f"vehicle = {_s(self.vehicle)}",
            f"zero_reference = {_s(self.zero_reference)}",
        ]
        if self.forward_gaze is not None:
            lines.append(f"forward_gaze = [{_f(self.forward_gaze[0])}, "
                         f"{_f(self.forward_gaze[1])}]   # pan_deg, tilt_deg")
        lines += [f"created_at = {_s(self.created_at)}", f"updated_at = {_s(self.updated_at)}"]
        for p in self.positions:
            lines += [
                "",
                "[[position]]",
                f"name = {_s(p.name)}",
                f"pan_deg = {_f(p.pan_deg)}",
                f"tilt_deg = {_f(p.tilt_deg)}",
                f"saved_at = {_s(p.saved_at)}",
            ]
        return "\n".join(lines) + "\n"

    @classmethod
    def from_toml(cls, text: str) -> "PositionProfile":
        raw = tomllib.loads(text)
        if raw.get("schema", SCHEMA) != SCHEMA:
            raise ProfileError(f"unknown profile schema {raw.get('schema')!r}")
        gaze = raw.get("forward_gaze")
        return cls(
            name=str(raw["name"]),
            vehicle=str(raw.get("vehicle", "")),
            zero_reference=str(raw.get("zero_reference", "")),
            forward_gaze=(float(gaze[0]), float(gaze[1])) if gaze else None,
            positions=tuple(
                Position(str(p["name"]), float(p["pan_deg"]), float(p["tilt_deg"]),
                         str(p.get("saved_at", "")))
                for p in raw.get("position", [])
            ),
            created_at=str(raw.get("created_at", "")),
            updated_at=str(raw.get("updated_at", "")),
        )


def _s(text: str) -> str:
    # A JSON string literal is a valid TOML basic string: same quote, backslash,
    # \n \t \b \f \r and \uXXXX escapes.
    return json.dumps(text, ensure_ascii=False)


def _f(value: float) -> str:
    if not math.isfinite(value):
        raise ProfileError(f"angle {value} is not finite")
    return repr(float(value))


class ProfileStore:
    """Position profiles on disk, one file per profile. Not thread-safe on its own;
    the controller serialises access."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _path(self, name: str) -> Path:
        return self.root / f"{slug(name)}.toml"

    def names(self) -> list[str]:
        if not self.root.is_dir():
            return []
        out = []
        for path in sorted(self.root.glob("*.toml")):
            try:
                out.append(PositionProfile.from_toml(path.read_text(encoding="utf-8")).name)
            except (tomllib.TOMLDecodeError, KeyError, ValueError) as exc:
                log.warning("skipping unreadable position profile %s: %s", path.name, exc)
        return out

    def load(self, name: str) -> PositionProfile:
        path = self._path(name)
        if not path.is_file():
            raise ProfileNotFound(f"no position profile {name!r}")
        return PositionProfile.from_toml(path.read_text(encoding="utf-8"))

    def save(self, profile: PositionProfile) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(profile.name)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(profile.to_toml(), encoding="utf-8")
        os.replace(tmp, path)
        return path

    def create(self, name: str, vehicle: str = "", zero_reference: str = "") -> PositionProfile:
        name = check_name(name, "profile")
        if self._path(name).exists():
            raise ProfileError(f"a profile named like {name!r} already exists")
        stamp = now_iso()
        profile = PositionProfile(name=name, vehicle=vehicle.strip(),
                                  zero_reference=zero_reference.strip(),
                                  created_at=stamp, updated_at=stamp)
        self.save(profile)
        return profile

    def delete(self, name: str) -> Path:
        """Move the profile to `.trash/` rather than deleting it: it may be the only record
        of how a past study was aimed."""
        path = self._path(name)
        if not path.is_file():
            raise ProfileNotFound(f"no position profile {name!r}")
        trash = self.root / ".trash"
        trash.mkdir(exist_ok=True)
        dest = trash / f"{path.stem}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.toml"
        os.replace(path, dest)
        return dest
