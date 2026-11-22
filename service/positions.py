"""Position profiles: the named directions a study visits, per vehicle configuration.

A position list is only worth anything if it replays identically on the next visit, so a
profile carries what makes that possible: the zero reference (where the fiducial sits in that
cab, how the rig is placed at the eye point) and the forward gaze. Angles are mount degrees
relative to that zero, recorded after the backlash was taken up along `approach_sign`.

Profiles are TOML files under `<data>/profiles/positions/`, so they can be read, diffed and
hashed into results. `tomllib` reads them. The standard library has no TOML writer, and this
schema is too small to justify a dependency, so `to_toml()` writes it directly.

Each position has a stable id, and its files live beside the profile in a folder named like
it: `<slug>/<id>.jpg` is the reference image (live view when it was saved; the canvas the mask
editor draws on) and `<slug>/<id>.masks.json` its masks, in the format
`analysis.detection.RegionMasks` reads. Masks belong to a position, not to the lens: they mark
this cab's windows and luminaires in this direction.
"""

from __future__ import annotations

import hashlib
import json
import uuid
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
    if "/" in name or "\\" in name:
        raise ProfileError(f"{what} name cannot contain / or \\ (names are part of URLs)")
    return name


def new_id() -> str:
    return uuid.uuid4().hex[:10]


def derived_id(name: str) -> str:
    """Id for a position saved before ids existed: stable across loads."""
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:10]


@dataclass(frozen=True)
class Position:
    name: str
    pan_deg: float
    tilt_deg: float
    saved_at: str = ""
    id: str = ""          # assigned when first added to a profile; names its files

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "pan_deg": self.pan_deg,
                "tilt_deg": self.tilt_deg, "saved_at": self.saved_at}


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
        """Replace the position of that name in place (keeping its id, so its masks stay
        attached), or append it with a new id."""
        check_name(pos.name, "position")
        out = list(self.positions)
        for i, p in enumerate(out):
            if p.name == pos.name:
                out[i] = replace(pos, id=p.id)
                break
        else:
            out.append(pos if pos.id else replace(pos, id=new_id()))
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

    def with_details(self, vehicle: str | None = None,
                     zero_reference: str | None = None) -> "PositionProfile":
        return replace(self,
                       vehicle=self.vehicle if vehicle is None else vehicle.strip(),
                       zero_reference=(self.zero_reference if zero_reference is None
                                       else zero_reference.strip()),
                       updated_at=now_iso())

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
                f"id = {_s(p.id)}",
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
                         str(p.get("saved_at", "")),
                         str(p.get("id") or derived_id(str(p["name"]))))
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


# -- masks -------------------------------------------------------------------- #

MASK_LIMITS = {"shapes": 100, "points": 200, "name": 60, "type": 40}


def _polygon(raw: Any, what: str) -> list[list[float]]:
    if not isinstance(raw, list) or not 3 <= len(raw) <= MASK_LIMITS["points"]:
        raise ProfileError(f"{what}: a polygon needs 3 to {MASK_LIMITS['points']} points")
    out = []
    for pt in raw:
        try:
            x, y = float(pt[0]), float(pt[1])
        except (TypeError, ValueError, IndexError, KeyError):
            raise ProfileError(f"{what}: points are [x, y] pairs") from None
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise ProfileError(f"{what}: coordinates are normalised 0..1, origin top left")
        out.append([round(x, 5), round(y, 5)])
    return out


def clean_masks(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate a masks document and keep only what detection reads. Polygons are
    normalised 0..1 from the top left, so they survive a change of binning."""
    if not isinstance(raw, dict):
        raise ProfileError("masks must be an object")
    out: dict[str, Any] = {"exclude": [], "glazing": [], "include": []}
    for kind in ("exclude", "glazing"):
        for i, poly in enumerate(raw.get(kind) or []):
            out[kind].append(_polygon(poly, f"{kind}[{i}]"))
    names: set[str] = set()
    for i, roi in enumerate(raw.get("include") or []):
        if not isinstance(roi, dict):
            raise ProfileError(f"include[{i}] must be an object")
        name = str(roi.get("name", "")).strip()
        if not name or len(name) > MASK_LIMITS["name"]:
            raise ProfileError(f"include[{i}]: luminaire name must be 1 to "
                               f"{MASK_LIMITS['name']} characters")
        if name in names:
            raise ProfileError(f"two luminaires named {name!r}: the name identifies the source")
        names.add(name)
        typ = roi.get("type")
        if typ is not None and (not isinstance(typ, str) or len(typ) > MASK_LIMITS["type"]):
            raise ProfileError(f"include[{i}]: type is a short text, e.g. white_led")
        item: dict[str, Any] = {"name": name, "type": typ or None,
                                "polygon": _polygon(roi.get("polygon"), f"include[{i}]")}
        refl = roi.get("reflects")
        if refl is not None and refl != "":
            if not isinstance(refl, str) or len(refl.strip()) > MASK_LIMITS["name"]                     or refl.strip() == name:
                raise ProfileError(f"include[{i}]: reflects names another luminaire (it may "
                                   "be in another position's frame)")
            item["reflects"] = refl.strip()
        thr = roi.get("threshold_cd_m2")
        if thr is not None:
            if isinstance(thr, bool) or not isinstance(thr, (int, float)) or not thr > 0:
                raise ProfileError(f"include[{i}]: threshold must be a positive number")
            item["threshold_cd_m2"] = float(thr)
        out["include"].append(item)
    if sum(len(v) for v in out.values()) > MASK_LIMITS["shapes"]:
        raise ProfileError(f"at most {MASK_LIMITS['shapes']} shapes per position")
    return out


def mask_counts(masks: dict[str, Any] | None) -> dict[str, Any]:
    m = masks or {}
    return {"exclude": len(m.get("exclude", [])), "glazing": len(m.get("glazing", [])),
            "include": len(m.get("include", [])), "version": m.get("version")}


class ProfileStore:
    """Position profiles on disk, one file per profile, plus a folder of per-position
    images and masks beside each. Not thread-safe on its own; the controller serialises
    access."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _path(self, name: str) -> Path:
        return self.root / f"{slug(name)}.toml"

    def assets(self, profile: str) -> Path:
        return self.root / slug(profile)

    def image_path(self, profile: str, pos: Position) -> Path:
        return self.assets(profile) / f"{pos.id}.jpg"

    def masks_path(self, profile: str, pos: Position) -> Path:
        return self.assets(profile) / f"{pos.id}.masks.json"

    def save_image(self, profile: str, pos: Position, jpeg: bytes) -> Path:
        return _atomic(self.image_path(profile, pos), jpeg)

    def load_masks(self, profile: str, pos: Position) -> dict[str, Any] | None:
        path = self.masks_path(profile, pos)
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def save_masks(self, profile: str, pos: Position, raw: dict[str, Any]) -> dict[str, Any]:
        """Validate and store. The version names the profile, the position and the
        content, and goes into the provenance of every result made with these masks."""
        masks = clean_masks(raw)
        digest = hashlib.sha256(json.dumps(masks, sort_keys=True).encode()).hexdigest()[:8]
        doc = {"version": f"{profile}/{pos.name}@{digest}", **masks,
               "position": {"name": pos.name, "pan_deg": pos.pan_deg,
                            "tilt_deg": pos.tilt_deg},
               "saved_at": now_iso()}
        _atomic(self.masks_path(profile, pos),
                json.dumps(doc, indent=2, ensure_ascii=False).encode("utf-8"))
        return doc

    def trash_position_files(self, profile: str, pos: Position) -> None:
        stamp = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
        for path in (self.image_path(profile, pos), self.masks_path(profile, pos)):
            if path.is_file():
                trash = self.root / ".trash" / slug(profile)
                trash.mkdir(parents=True, exist_ok=True)
                os.replace(path, trash / f"{stamp}-{path.name}")

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
        stamp = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
        dest = trash / f"{path.stem}-{stamp}.toml"
        os.replace(path, dest)
        if self.assets(name).is_dir():  # its images and masks go with it
            os.replace(self.assets(name), trash / f"{path.stem}-{stamp}")
        return dest


def _atomic(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return path
