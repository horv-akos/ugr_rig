"""Camera control - Canon EOS 2000D over USB with libgphoto2 (PTP).

Same pattern as the mount: `Camera` is a small protocol, `Gphoto2Camera`
drives the real body, `hal.camera_sim.FakeCamera` renders a synthetic scene on
a desk. Both produce a bracket directory with a `manifest.json` that the
photometry layer reads, so nothing above `hal` knows which one took the frames.

What the code cannot do on a 2000D, and therefore checks instead:
  - the mode dial is physical: must be on M
  - AF/MF is the lens switch: must be on MF (AF hunting in a dark cab blocks
    the shutter, and refocusing between frames would move the image)
Things to switch off on the body once, by hand (not exposed over PTP):
  - Highlight Tone Priority (lifts the minimum ISO and changes the raw exposure)
  - Long exposure noise reduction (doubles the time of the 1 s and 4 s frames;
    harmless to the data, but slow)

python-gphoto2 is LGPL-3.0-or-later and wraps libgphoto2 (LGPL-2.1). It is
imported inside Gphoto2Camera so this module loads on a machine without it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .camera_config import (
    CameraConfig,
    effective_time,
    nearest_choice,
    parse_aperture,
    parse_shutter,
)

log = logging.getLogger(__name__)

MANIFEST = "manifest.json"


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #


class CameraError(RuntimeError):
    pass


class CameraNotFound(CameraError):
    """No camera on USB, or it is switched off / asleep."""


class CameraBusy(CameraError):
    """Another process holds the USB interface (often gvfs on a desktop image)."""


class CameraDisconnected(CameraError):
    """USB link dropped mid-operation. Completed brackets on disk are intact."""


class CameraSetupError(CameraError):
    """Body is in a state the code cannot fix: mode dial, AF switch, missing setting."""


class BracketError(CameraError):
    """A frame did not verify: truncated download, or EXIF disagrees with the request."""


# --------------------------------------------------------------------------- #
# bracket manifest
# --------------------------------------------------------------------------- #


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class FrameRecord:
    file: str                 # relative to the bracket directory
    shutter_label: str        # as the camera names it, e.g. "1/60"
    t_marked_s: float         # the label as a number
    t_effective_s: float      # what the merge uses (apex model + correction)
    aperture: float
    iso: int
    bytes: int
    sha256: str
    captured_at: str
    exif: dict[str, Any] = field(default_factory=dict)


@dataclass
class BracketManifest:
    format: str                               # "cr2" | "npz" (simulator)
    camera: dict[str, Any]
    settings: dict[str, Any]
    frames: list[FrameRecord] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    meta: dict[str, Any] = field(default_factory=dict)   # region name, pan/tilt, ...
    warnings: list[str] = field(default_factory=list)

    def save(self, directory: str | Path) -> Path:
        path = Path(directory) / MANIFEST
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2))
        os.replace(tmp, path)
        return path

    @classmethod
    def load(cls, directory: str | Path) -> "BracketManifest":
        raw = json.loads((Path(directory) / MANIFEST).read_text())
        frames = [FrameRecord(**f) for f in raw.pop("frames", [])]
        return cls(frames=frames, **raw)


# --------------------------------------------------------------------------- #
# protocol
# --------------------------------------------------------------------------- #


@runtime_checkable
class Camera(Protocol):
    config: CameraConfig

    def connect(self) -> None: ...
    def close(self) -> None: ...
    def info(self) -> dict[str, Any]: ...
    def capture_bracket(self, out_dir: str | Path,
                        meta: dict[str, Any] | None = None) -> BracketManifest: ...
    def preview(self) -> bytes: ...       # one live-view JPEG
    def end_preview(self) -> None: ...    # leave live view (lower the mirror)


def create_camera(kind: str, config: CameraConfig, **kwargs) -> Camera:
    if kind == "gphoto2":
        return Gphoto2Camera(config, **kwargs)
    if kind == "sim":
        from .camera_sim import FakeCamera
        return FakeCamera(config, **kwargs)
    raise ValueError(f"unknown camera backend {kind!r}")


# --------------------------------------------------------------------------- #
# frame verification (shared by the real camera and tools)
# --------------------------------------------------------------------------- #


def read_exif(path: Path) -> dict[str, Any]:
    """Shutter / aperture / ISO as the camera recorded them. Header only, fast."""
    import exifread

    with open(path, "rb") as fh:
        tags = exifread.process_file(fh, details=False)

    def num(tag: str) -> float | None:
        v = tags.get(tag)
        if v is None or not v.values:
            return None
        x = v.values[0]
        return float(x.num) / float(x.den) if hasattr(x, "num") else float(x)

    return {
        "exposure_time_s": num("EXIF ExposureTime"),
        "f_number": num("EXIF FNumber"),
        "iso": num("EXIF ISOSpeedRatings"),
        "model": str(tags.get("Image Model", "")).strip(),
        "datetime": str(tags.get("EXIF DateTimeOriginal", "")).strip(),
    }


def check_exif(exif: dict[str, Any], label: str, aperture: float, iso: int) -> list[str]:
    """Return a list of disagreements between what was asked and what was shot."""
    problems = []
    t = exif.get("exposure_time_s")
    if t is None or abs(t / parse_shutter(label) - 1) > 0.02:
        problems.append(f"shutter {t} s recorded, {label} requested - is the dial on M?")
    n = exif.get("f_number")
    if n is None or abs(n - aperture) > 0.15:
        problems.append(f"f/{n} recorded, f/{aperture:g} requested")
    i = exif.get("iso")
    if i is None or int(round(i)) != int(iso):
        problems.append(f"ISO {i} recorded, {iso} requested - Highlight Tone Priority on?")
    return problems


# --------------------------------------------------------------------------- #
# gphoto2
# --------------------------------------------------------------------------- #


class Gphoto2Camera:
    """Canon EOS over libgphoto2.

    Config names drift between Canon generations; the first name present on the
    body wins. `ugrctl camera info` prints what this body actually exposes.
    """

    KEYS: dict[str, tuple[str, ...]] = {
        "shutter": ("shutterspeed",),
        "aperture": ("aperture",),
        "iso": ("iso",),
        "format": ("imageformat",),
        "wb": ("whitebalance",),
        "target": ("capturetarget",),
        "mode": ("autoexposuremodedial", "autoexposuremode"),
        "focus": ("focusmode",),
        "drive": ("drivemode",),
        "battery": ("batterylevel",),
        "serial": ("eosserialnumber", "serialnumber"),
        "lens": ("lensname",),
        "model": ("cameramodel", "model"),
        "autopoweroff": ("autopoweroff",),
        "viewfinder": ("viewfinder", "eosviewfinder"),  # live view on/off (toggle)
    }
    TARGET_CHOICES = {"ram": "Internal RAM", "card": "Memory card"}
    BUSY_RETRIES = 10

    def __init__(self, config: CameraConfig) -> None:
        self.config = config
        self._gp = None
        self._cam = None
        self._names: dict[str, str] = {}
        self._log_cb = None
        self._choice_cache: dict[str, list[str]] = {}
        self._preview_on = False

    # -- connection ---------------------------------------------------------- #

    def connect(self) -> None:
        import gphoto2 as gp

        self._gp = gp
        self._log_cb = gp.use_python_logging()  # keep a reference or logging stops
        cam = gp.Camera()
        try:
            cam.init()
        except gp.GPhoto2Error as exc:
            raise self._translate(exc, "connect") from exc
        self._cam = cam
        self._names = self._resolve_names()
        self._check_body()
        self._apply_static_settings()
        log.info("connected: %s", self.info())

    def close(self) -> None:
        if self._cam is not None:
            try:
                self._cam.exit()
            except Exception:  # noqa: BLE001 - closing a dead link is fine
                pass
        self._cam = None
        self._preview_on = False

    def reconnect(self) -> None:
        self.close()
        time.sleep(1.0)
        self.connect()

    def __enter__(self) -> "Gphoto2Camera":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- low level ----------------------------------------------------------- #

    def _translate(self, exc: Exception, what: str) -> CameraError:
        gp = self._gp
        code = getattr(exc, "code", None)
        c = lambda name: getattr(gp, name, None)  # noqa: E731
        if code in (c("GP_ERROR_MODEL_NOT_FOUND"), c("GP_ERROR_IO_USB_FIND")):
            return CameraNotFound(f"{what}: no camera on USB - powered on, cable in, not asleep?")
        if code == c("GP_ERROR_IO_USB_CLAIM"):
            return CameraBusy(f"{what}: USB interface claimed by another process "
                              "(gvfs-gphoto2-volume-monitor on a desktop image?)")
        if code == c("GP_ERROR_CAMERA_BUSY"):
            return CameraBusy(f"{what}: camera busy")
        if code is not None and (code in (c("GP_ERROR_IO"), c("GP_ERROR_TIMEOUT"))
                                 or -60 <= code <= -20):
            return CameraDisconnected(f"{what}: USB link lost ({exc})")
        return CameraError(f"{what}: {exc}")

    def _call(self, what: str, fn, *args):
        """Run a gphoto2 call, retrying while the body reports busy."""
        gp = self._gp
        for attempt in range(self.BUSY_RETRIES):
            try:
                return fn(*args)
            except gp.GPhoto2Error as exc:
                if exc.code == gp.GP_ERROR_CAMERA_BUSY and attempt < self.BUSY_RETRIES - 1:
                    time.sleep(0.3)
                    continue
                raise self._translate(exc, what) from exc
        raise CameraBusy(what)  # pragma: no cover

    def _require(self):
        if self._cam is None:
            raise CameraError("not connected")
        return self._cam

    def _resolve_names(self) -> dict[str, str]:
        cam = self._require()
        tree = self._call("read config", cam.get_config)
        names = {}
        for key, candidates in self.KEYS.items():
            for name in candidates:
                try:
                    tree.get_child_by_name(name)
                except self._gp.GPhoto2Error:
                    continue
                names[key] = name
                break
        return names

    def _widget(self, key: str):
        cam = self._require()
        name = self._names.get(key)
        if name is None:
            raise CameraSetupError(f"body exposes no '{key}' setting "
                                   f"(tried {', '.join(self.KEYS[key])})")
        if hasattr(cam, "get_single_config"):
            try:
                return name, cam.get_single_config(name)
            except self._gp.GPhoto2Error as exc:
                if exc.code != self._gp.GP_ERROR_NOT_SUPPORTED:
                    raise self._translate(exc, f"read {name}") from exc
        tree = self._call("read config", cam.get_config)
        return name, tree.get_child_by_name(name)

    def _write(self, name: str, widget) -> None:
        cam = self._require()
        if hasattr(cam, "set_single_config"):
            try:
                cam.set_single_config(name, widget)
                return
            except self._gp.GPhoto2Error as exc:
                if exc.code == self._gp.GP_ERROR_CAMERA_BUSY:
                    raise
                if exc.code != self._gp.GP_ERROR_NOT_SUPPORTED:
                    raise self._translate(exc, f"set {name}") from exc
        # old libgphoto2: write the whole tree back
        tree = cam.get_config()
        tree.get_child_by_name(name).set_value(widget.get_value())
        cam.set_config(tree)

    def _get(self, key: str, default: Any = None) -> Any:
        if key not in self._names:
            return default
        _, w = self._widget(key)
        return w.get_value()

    def _choices(self, key: str) -> list[str]:
        if key not in self._choice_cache:
            _, w = self._widget(key)
            try:
                self._choice_cache[key] = [str(c) for c in w.get_choices()]
            except self._gp.GPhoto2Error:
                self._choice_cache[key] = []
        return self._choice_cache[key]

    def _set(self, key: str, value: str) -> None:
        name, w = self._widget(key)
        if w.get_readonly():
            raise CameraSetupError(f"{name} is read-only on this body")
        choices = self._choices(key)
        if choices and value not in choices:
            raise CameraSetupError(f"{name}: {value!r} not offered (choices: {choices})")
        if str(w.get_value()) == value:
            return
        w.set_value(value)
        self._call(f"set {name}={value}", self._write, name, w)
        self._drain_events(0.05)
        got = str(self._widget(key)[1].get_value())
        if got != value:
            raise CameraSetupError(f"{name}: wrote {value!r}, body reports {got!r}")

    def _drain_events(self, settle_s: float = 0.1) -> list[tuple[int, Any]]:
        """Canon queues events after every change; leaving them makes the next call busy."""
        gp, cam = self._gp, self._require()
        events = []
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            etype, data = self._call("events", cam.wait_for_event, int(settle_s * 1000))
            if etype == gp.GP_EVENT_TIMEOUT:
                break
            events.append((etype, data))
        return events

    # -- setup --------------------------------------------------------------- #

    def _check_body(self) -> None:
        mode = self._get("mode")
        if mode is None:
            log.warning("cannot read the exposure mode; make sure the dial is on M")
        elif str(mode).lower() not in ("manual", "m"):
            raise CameraSetupError(f"mode dial is on {mode!r} - set it to M")
        focus = self._get("focus")
        if focus is not None and str(focus).lower() not in ("manual", "mf"):
            # Not every EOS reports the lens switch here, so this is a warning.
            log.warning("focus mode reads %r - the lens switch must be on MF", focus)
        model = str(self._get("model", ""))
        if model and self.config.model.lower() not in model.lower():
            log.warning("expected %s, body reports %s", self.config.model, model)

    def _apply_static_settings(self) -> None:
        cfg = self.config
        self._set("target", self.TARGET_CHOICES[cfg.capture_target])
        self._set("format", cfg.image_format)
        self._set("iso", str(cfg.exposure.iso))
        ap = [c for c in self._choices("aperture")
              if _safe(parse_aperture, c) is not None
              and abs(parse_aperture(c) - cfg.exposure.aperture) < 0.05]
        if not ap:
            raise CameraSetupError(f"f/{cfg.exposure.aperture:g} not offered by this lens "
                                   f"(choices: {self._choices('aperture')})")
        self._set("aperture", ap[0])
        if "wb" in self._names:
            try:
                self._set("wb", cfg.white_balance)
            except CameraSetupError as exc:
                log.warning("white balance not set: %s", exc)
        if "autopoweroff" in self._names:
            try:
                self._set("autopoweroff", "0")
            except CameraSetupError:
                log.warning("could not disable auto power off; set it to Disable on the body")
        # check every bracket speed exists before the first frame, not halfway through
        for label in cfg.exposure.shutter:
            nearest_choice(parse_shutter(label), self._choices("shutter"))

    def info(self) -> dict[str, Any]:
        out = {k: self._get(k) for k in
               ("model", "serial", "lens", "battery", "mode", "focus", "target",
                "format", "iso", "aperture", "shutter")}
        out["config_names"] = dict(self._names)
        return {k: (str(v) if v is not None else None) if not isinstance(v, dict) else v
                for k, v in out.items()}

    def choices(self) -> dict[str, list[str]]:
        return {k: self._choices(k) for k in ("shutter", "aperture", "iso", "format", "target")
                if k in self._names}

    # -- live view ----------------------------------------------------------- #

    def preview(self) -> bytes:
        """One live-view frame, as the JPEG the body sends; nothing is decoded here.

        On a Canon EOS the first call raises the mirror into live view, and it stays up
        until end_preview(). Live view warms the sensor, and the extra dark current shows
        in the long bracket frames, so the bracket leaves it first and the service keeps it
        off for a recording run.
        """
        cam = self._require()
        cf = self._call("live view", cam.capture_preview)
        self._preview_on = True
        return bytes(memoryview(cf.get_data_and_size()))

    def end_preview(self) -> None:
        """Leave live view. A no-op when it is not on, or the body has no switch for it."""
        if not self._preview_on or self._cam is None:
            return
        self._preview_on = False
        if "viewfinder" not in self._names:
            return
        try:
            name, w = self._widget("viewfinder")
            w.set_value(0)
            self._call(f"set {name}=0", self._write, name, w)
            self._drain_events(0.05)
        except CameraError as exc:
            log.warning("could not leave live view: %s", exc)

    # -- capture ------------------------------------------------------------- #

    def capture_bracket(self, out_dir: str | Path,
                        meta: dict[str, Any] | None = None) -> BracketManifest:
        """Shoot the configured fixed bracket into out_dir and verify every frame.

        On a dropped link this raises CameraDisconnected; frames already written
        stay on disk, but the bracket is incomplete and must be re-shot whole -
        a merge from a partial bracket would silently lose dynamic range.
        """
        cfg = self.config
        self.end_preview()  # never shoot the bracket from live view: mirror down, sensor cooler
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        manifest = BracketManifest(
            format="cr2",
            camera={k: v for k, v in self.info().items() if k in ("model", "serial", "lens")},
            settings=cfg.snapshot(),
            started_at=now_iso(),
            meta=dict(meta or {}),
        )
        shutter_choices = self._choices("shutter")
        # Shortest first: if the link drops, the frames that protect the bright
        # sources from clipping are already safe.
        order = sorted(cfg.exposure.shutter, key=parse_shutter)
        for i, label in enumerate(order):
            choice = nearest_choice(parse_shutter(label), shutter_choices)
            rec = self._capture_frame(out, i, choice)
            manifest.frames.append(rec)
            manifest.save(out)  # partial manifest marks how far we got
        manifest.finished_at = now_iso()
        manifest.save(out)
        return manifest

    def _capture_frame(self, out: Path, index: int, label: str) -> FrameRecord:
        gp, cam, cfg = self._gp, self._require(), self.config
        self._set("shutter", label)
        captured_at = now_iso()
        path = self._call(f"capture {label}", cam.capture, gp.GP_CAPTURE_IMAGE)
        self._drain_events(0.1)

        ext = Path(path.name).suffix.lower() or ".cr2"
        if ext != ".cr2":
            raise BracketError(f"camera wrote {path.name}, expected a CR2 - image format?")
        info = self._call("file info", cam.file_get_info, path.folder, path.name)
        expected = int(info.file.size)
        name = f"frame_{index:02d}{ext}"
        dest = out / name
        tmp = dest.with_suffix(".part")
        cf = self._call(f"download {path.name}", cam.file_get,
                        path.folder, path.name, gp.GP_FILE_TYPE_NORMAL)
        cf.save(str(tmp))
        with open(tmp, "rb+") as fh:
            os.fsync(fh.fileno())
        size = tmp.stat().st_size
        if size != expected or size < cfg.min_file_bytes:
            raise BracketError(f"{path.name}: {size} bytes on the Pi, {expected} on the camera")
        os.replace(tmp, dest)

        exif = read_exif(dest) if cfg.exif_check else {}
        if cfg.exif_check:
            problems = check_exif(exif, label, cfg.exposure.aperture, cfg.exposure.iso)
            if problems:
                raise BracketError(f"{name}: " + "; ".join(problems))
        _verify_raw(dest)

        # The Pi copy is verified; only now may the card copy go.
        if cfg.capture_target == "ram" or cfg.delete_after_verify:
            try:
                self._call("delete", cam.file_delete, path.folder, path.name)
            except CameraError as exc:
                log.warning("could not delete %s/%s from camera: %s", path.folder, path.name, exc)

        return FrameRecord(
            file=name,
            shutter_label=label,
            t_marked_s=parse_shutter(label),
            t_effective_s=effective_time(label, cfg.exposure.shutter_model,
                                         cfg.exposure.shutter_correction),
            aperture=cfg.exposure.aperture,
            iso=cfg.exposure.iso,
            bytes=size,
            sha256=sha256_file(dest),
            captured_at=captured_at,
            exif=exif,
        )


def _verify_raw(path: Path) -> None:
    """Open the CR2 with LibRaw; a file that does not decode is not a frame."""
    try:
        import rawpy
    except ImportError:
        log.warning("rawpy missing: %s not decoded during verification", path.name)
        return
    try:
        with rawpy.imread(str(path)) as raw:
            if raw.raw_image_visible.size == 0:
                raise BracketError(f"{path.name}: empty raw image")
    except (rawpy.LibRawError, OSError) as exc:
        raise BracketError(f"{path.name}: does not decode ({exc})") from exc


def _safe(fn, *args):
    try:
        return fn(*args)
    except (ValueError, TypeError):
        return None
