"""Rig controller: the one owner of the hardware.

The head is open-loop, so its position exists only in the memory of the process that moved
it, and only one process can hold the camera's USB session. So one long-running process holds
one RigController, and the web pages and `rigctl` are clients of its HTTP API. A CLI that
opened the mount itself would lose the position the moment it exited.

One motion at a time, each in its own worker thread, so the API can answer (status, stop)
while the head moves. Every motion carries its own stop event, created before the thread
starts, so a stop that arrives before the move begins is not lost.

Hold-to-move over WiFi needs a dead-man: while a jog runs the client pings, and a watchdog
ramps the jog down when the pings stop. Phones drop WiFi and lock their screens mid-press.

The camera belongs here too, for the same one-owner reason. Today it serves live view
(`service.liveview`); bracket capture will take it over through `LiveView.paused()`.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from hal.backend import SimBackend, create_backend
from hal.config import MountConfig, load_config
from hal.mount import MountError, MoveResult, NotZeroed, PanTiltMount
from storage import StudyIndex, StudyStore

from .camera_profiles import CameraProfile, CameraProfileStore, KRecord
from .liveview import LiveView
from .positions import (Position, PositionProfile, ProfileNotFound, ProfileStore, check_name,
                        mask_counts, now_iso, slug)
from .study import LIGHTING, StudyRun, new_record, study_id_for

REPO = Path(__file__).resolve().parents[1]

log = logging.getLogger(__name__)

# Operator-facing direction names. Mount convention: pan positive = camera turns right,
# tilt positive = camera looks up.
DIRECTIONS: dict[str, tuple[str, int]] = {
    "left": ("pan", -1),
    "right": ("pan", 1),
    "up": ("tilt", 1),
    "down": ("tilt", -1),
}
HEARTBEAT_TIMEOUT_S = 0.3   # jog ramps down when the client has not pinged for this long
WATCHDOG_PERIOD_S = 0.05
HERE_DEG = 0.05            # the head counts as at a saved position within this

DEFAULT_DATA_DIR = Path(__file__).resolve().parents[1] / "data"


class RigError(RuntimeError):
    pass


class RigBusy(RigError):
    """Another motion is running. Stop it first."""


@dataclass
class Motion:
    id: int
    kind: str                        # jog | nudge | goto | zero | backlash | save | run
    detail: dict[str, Any]
    stop: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    started_at: float = field(default_factory=time.monotonic)
    ended_at: float | None = None
    result: dict[str, Any] | None = None
    error: Exception | None = None
    stop_reason: str | None = None   # who asked it to stop, if anyone

    def summary(self) -> dict[str, Any]:
        end = self.ended_at or time.monotonic()
        return {
            "id": self.id,
            "kind": self.kind,
            "detail": dict(self.detail),
            "running": not self.done.is_set(),
            "elapsed_s": round(end - self.started_at, 3),
            "stop_reason": self.stop_reason,
            "result": self.result,
            "error": None if self.error is None else
            {"type": type(self.error).__name__, "detail": str(self.error)},
        }


class RigController:
    def __init__(self, mount: PanTiltMount, data_dir: str | Path = DEFAULT_DATA_DIR,
                 live: LiveView | None = None,
                 geometry: dict[str, Any] | None = None) -> None:
        self.mount = mount
        self.live = live or LiveView(None)
        self._geometry = geometry
        self.data_dir = Path(data_dir)
        self.profiles = ProfileStore(self.data_dir / "profiles" / "positions")
        self._state_path = self.data_dir / "state.json"
        self._lock = threading.RLock()          # motion bookkeeping
        self._profiles_lock = threading.RLock()  # profile files
        self._motion: Motion | None = None
        self._last: Motion | None = None
        self._next_id = 1
        self._speed = 1.0
        self._last_ping = 0.0
        self._last_known = self._read_state()
        self.camera_profiles = CameraProfileStore(self.data_dir / "profiles" / "camera")
        self.camera_profiles.ensure_default()
        self.calibrations_dir = self.data_dir / "calibrations"
        self.lensfits_dir = self.data_dir / "lensfits"
        self._zero_info: dict[str, Any] | None = None   # how the current zero was set
        self._last_check: dict[str, Any] | None = None  # the last fiducial check
        self.studies = StudyStore(self.data_dir / "studies")
        self.index = StudyIndex(self.data_dir / "index.sqlite")
        self._runs: dict[str, StudyRun] = {}
        self._pool = None
        self.software = software_version()
        self._recover_studies()
        self.index.rebuild(self.studies)   # the folders are the record; the index follows
        self._closing = threading.Event()
        self._watchdog = threading.Thread(target=self._watch, name="rig-watchdog", daemon=True)
        self._watchdog.start()

    @classmethod
    def from_config(cls, backend: str = "sim", data_dir: str | Path = DEFAULT_DATA_DIR,
                    mount_config: str | Path | None = None, camera: str = "none",
                    **backend_kwargs) -> "RigController":
        """`camera`: "gphoto2" (the 2000D), "sim" (a synthetic cab that follows the simulated
        head; needs backend "sim"), or "none" (no live view)."""
        cfg = load_config(mount_config)
        pulses = create_backend(backend, **backend_kwargs)
        geometry = camera_geometry()
        cam = None
        if camera == "gphoto2":
            from hal.camera import Gphoto2Camera
            from hal.camera_config import load_camera_config
            cam = Gphoto2Camera(load_camera_config())
        elif camera == "sim":
            if not isinstance(pulses, SimBackend):
                raise ValueError("the simulated camera follows the simulated head: use backend sim")
            from hal.camera_config import load_camera_config
            from hal.camera_sim import CabView, FakeCamera
            pulses.record_edges = False  # a long-running server must not keep every edge
            fov = tuple(geometry["fov_deg"])
            # Brackets of the simulated cab are small (450 x 300 superpixels) so a study
            # runs in seconds; processing is told the matching pixel pitch.
            cam = FakeCamera(load_camera_config(), pose=sim_pose(pulses, cfg),
                             view=CabView(fov), bracket_view=CabView(fov, size=(450, 300)))
        elif camera != "none":
            raise ValueError(f"unknown camera {camera!r}: gphoto2, sim or none")
        from hal.tmc2209 import create_drivers

        # Real hardware always gets the UART; the simulator gets a simulated driver pair, so
        # the configure-and-verify path runs on every desk start too.
        drivers = create_drivers("uart" if backend == "pigpio" else "sim", cfg.drivers,
                                 (cfg.pan, cfg.tilt))
        live = LiveView(cam, camera)
        return cls(PanTiltMount(cfg, pulses, drivers), data_dir, live, geometry)

    def close(self) -> None:
        """Stop the watchdog and live view, cut any motion and release the drivers."""
        self._closing.set()
        m = self._motion
        if m is not None:
            m.stop.set()
            self.mount.abort()
            m.done.wait(2.0)
        self.live.close()
        self.mount.close()
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
        self.index.close()

    # -- studies -------------------------------------------------------------- #

    @property
    def speed(self) -> float:
        return self._speed

    def pool(self):
        """One worker process for analysis, started on first use: numpy in the server
        process would stall live view and the jog heartbeat."""
        if self._pool is None:
            import multiprocessing
            from concurrent.futures import ProcessPoolExecutor
            self._pool = ProcessPoolExecutor(max_workers=1,
                                             mp_context=multiprocessing.get_context("spawn"))
        return self._pool

    def processing_setup(self, camera_profile: str | None = None
                         ) -> tuple[float | None, dict[str, Any], list[str]]:
        """(k, config overrides, notes) for analysing brackets shot with `camera_profile`
        (the active one by default): its lens, k and spectral factors override
        config/analysis.toml."""
        from photometry.profile import load_analysis_config

        cfg = load_analysis_config()
        prof = self.camera_profile(camera_profile)
        overrides = prof.analysis_overrides()
        notes = [f"camera profile {prof.name} ({prof.content_hash[:10]})"]
        k = prof.k or None
        cam = self.live.camera
        bracket_view = getattr(cam, "bracket_view", None)
        if self.live.source == "sim" and bracket_view is not None:
            w = bracket_view.size[0]
            sensor_mm = cfg.sensor.width_px * cfg.sensor.pixel_pitch_um / 1000
            overrides["sensor.pixel_pitch_um"] = sensor_mm * 1000 / (cfg.processing.binning * w)
            overrides["lens.vignetting_map"] = ""
            notes.append("simulated camera: pixel pitch scaled so its small frames keep the "
                         "2000D field of view")
            if not k:
                k = cam.k_true
                overrides["calibration.k"] = k
                notes.append("simulated camera, profile not calibrated: k is the simulator's")
        return k, overrides, notes

    # -- camera profiles and calibration ------------------------------------- #

    @property
    def geometry(self) -> dict[str, Any]:
        """Field of view of the active camera profile's lens (live-view overlays)."""
        if self._geometry is not None:
            return self._geometry
        p = self.camera_profile()
        return camera_geometry(p.focal_mm, p.distortion_k1)

    def _active_path(self) -> Path:
        return self.camera_profiles.root / ".active"

    def camera_profile(self, name: str | None = None) -> CameraProfile:
        if name is None:
            try:
                name = self._active_path().read_text(encoding="utf-8").strip()
            except OSError:
                name = ""
            if name not in self.camera_profiles.names():
                name = self.camera_profiles.names()[0]
        return self.camera_profiles.load(name)

    def set_active_camera_profile(self, name: str) -> CameraProfile:
        p = self.camera_profiles.load(name)
        self._active_path().write_text(p.name, encoding="utf-8")
        return p

    def camera_info(self) -> dict[str, Any]:
        """What the body reports (model, serial, lens, mode...), and how it compares with
        the active camera profile."""
        if self.live.camera is None:
            raise RigError("no camera: start the server with --camera sim or gphoto2")
        with self._lock:
            self._require_idle()
            with self.live.paused() as cam:
                info = cam.info()
        prof = self.camera_profile()
        return {"info": info, "profile": prof.name, "mismatch": prof.body_check(info)}

    def calibrate(self, profile: str, roi: list[float], luminance: float | None = None,
                  lux: float | None = None, rho: float | None = None, method: str = "meter",
                  q: float | None = None, iso_ratio: float | None = None) -> Motion:
        """Shoot a bracket of the matt target and measure k inside `roi` (normalised x0, y0,
        x1, y1) against the meter: a luminance reading, or illuminance on a panel of
        reflectance rho (L = rho E / pi). The result is offered, not applied: the operator
        accepts it into the profile (`accept_calibration`).

        method "iso": no meter. k is estimated from the camera's ISO rating (ISO 12232)
        with one frame of a neutral surface in `roi` (photometry.calibration.iso_estimate);
        q and iso_ratio default to config/analysis.toml."""
        from hal.camera_config import load_camera_config
        from photometry.calibration import target_luminance
        from photometry.profile import load_analysis_config
        from pipeline import study as worker

        prof = self.camera_profiles.load(profile)
        if method == "iso":
            cal = load_analysis_config().calibration
            q = cal.iso_q if q is None else float(q)
            iso_ratio = cal.iso_saturation_ratio if iso_ratio is None else float(iso_ratio)
            if not (0.2 <= q <= 1.0 and 0.25 <= iso_ratio <= 4.0):
                raise ValueError("q must be 0.2 to 1, the saturation ISO ratio 0.25 to 4")
            target = None
            meter: dict[str, Any] = {"q": q, "iso_ratio": iso_ratio}
        elif method == "meter":
            target = target_luminance(luminance, lux, rho)
            if not target > 0:
                raise ValueError("the target luminance must be positive")
            meter = {"luminance": luminance, "lux": lux, "rho": rho}
        else:
            raise ValueError("method must be meter or iso")
        if len(roi) != 4 or not (0 <= roi[0] < roi[2] <= 1 and 0 <= roi[1] < roi[3] <= 1):
            raise ValueError("ROI is x0, y0, x1, y1, normalised 0..1 from the top left")
        with self._lock:
            self._require_idle()
            if self.live.camera is None:
                raise RigError("no camera: start the server with --camera sim or gphoto2")
            cid = f"{now_iso().replace(':', '').replace('-', '')}-{slug(prof.name)}"
            d = self.calibrations_dir / cid

            def run(stop: threading.Event) -> dict[str, Any]:
                with self.live.paused() as cam:
                    cam.apply_config(prof.camera_config(load_camera_config()))
                    mismatch = prof.body_check(cam.info())
                    pan, tilt = self.mount.position()
                    cam.capture_bracket(d / "bracket", meta={
                        "region": "calibration target", "pan_deg": round(pan, 4),
                        "tilt_deg": round(tilt, 4), "calibration": cid})
                _, overrides, notes = self.processing_setup(prof.name)
                overrides["calibration.k"] = prof.k     # drift against the stored value only
                m = self.pool().submit(worker.process_calibration, {
                    "bracket": str(d / "bracket"), "roi": list(roi), "target_cd_m2": target,
                    "method": method, "q": q, "iso_ratio": iso_ratio,
                    "overrides": overrides}).result()
                record = {"id": cid, "at": now_iso(), "profile": prof.name,
                          "profile_hash": prof.content_hash, "method": method,
                          "meter": meter,
                          "measurement": m, "body_mismatch": mismatch, "notes": notes,
                          "accepted_at": None}
                (d / "calibration.json").write_text(json.dumps(record, indent=2),
                                                    encoding="utf-8")
                return record

            return self._start("calibrate", {"profile": prof.name, "calibration": cid}, run)

    def calibrations(self, profile: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        out = []
        if self.calibrations_dir.is_dir():
            for f in sorted(self.calibrations_dir.glob("*/calibration.json"), reverse=True):
                rec = json.loads(f.read_text(encoding="utf-8"))
                if profile is None or rec["profile"] == profile:
                    out.append(rec)
                if len(out) >= limit:
                    break
        return out

    def accept_calibration(self, profile: str, calibration: str) -> CameraProfile:
        """Make a measured k the profile's k. The old value stays in the profile's history."""
        f = self.calibrations_dir / calibration / "calibration.json"
        if "/" in calibration or "\\" in calibration or not f.is_file():
            raise ProfileNotFound(f"no calibration {calibration!r}")
        rec = json.loads(f.read_text(encoding="utf-8"))
        if rec["profile"] != profile:
            raise RigError(f"calibration {calibration} was made with profile {rec['profile']}")
        m = rec["measurement"]
        iso = rec.get("method") == "iso"
        note = (f"ISO 12232 estimate: q {m['q']:g}, saturation ISO / label {m['iso_ratio']:g}, "
                f"DN_sat {m['dn_sat']:.0f} ({m['first_clip']} clips first)") if iso else ""
        with self._profiles_lock:
            p = self.camera_profiles.load(profile).with_k(KRecord(
                at=rec["at"], k=m["k"], target_cd_m2=m.get("target_cd_m2"),
                roi=tuple(m["roi"]), cv=m["cv"], drift=m["drift"], calibration=calibration,
                source="iso" if iso else "target", note=note))
            self.camera_profiles.save(p)
        rec["accepted_at"] = now_iso()
        f.write_text(json.dumps(rec, indent=2), encoding="utf-8")
        return p

    def set_k(self, profile: str, k: float, note: str) -> CameraProfile:
        """Type k in by hand (no meter at hand). It replaces the stored k, whatever it was;
        the old value stays in the profile's history."""
        with self._profiles_lock:
            p = self.camera_profiles.load(profile).with_typed_k(k, note)
            self.camera_profiles.save(p)
        return p

    def start_study(self, profile: str, lighting: str, operator: str = "", notes: str = "",
                    sweep: bool = True, check: bool = True,
                    camera_profile: str | None = None) -> tuple[Motion, str]:
        """Record a study of every position in `profile` with `camera_profile` (the active
        one by default); analysis follows on its own."""
        if lighting not in LIGHTING:
            raise ValueError(f"lighting must be one of {LIGHTING}")
        cam_prof = self.camera_profile(camera_profile)
        p = self.get_profile(profile)
        if not p.positions:
            raise RigError(f"profile {profile!r} has no positions to record")
        with self._lock:
            self._require_idle()
            self._require_enabled()
            self._require_zero()
            if self.live.camera is None:
                raise RigError("no camera: start the server with --camera sim or gphoto2")
            sid = study_id_for(self, p.name)
            run = StudyRun(self, new_record(sid, p, lighting, operator, notes, sweep, check,
                                            self.software, cam_prof))
            run.prepare(p, cam_prof)
            self._runs[sid] = run
            motion = self._start("study", {"study": sid, "profile": p.name}, run.capture)
        threading.Thread(target=run.analyse, name=f"analysis-{sid}", daemon=True).start()
        return motion, sid

    def reanalyse(self, study_id: str) -> dict[str, Any]:
        """Analyse a finished study again from its own brackets: after calibrating k, or
        after a change to the analysis. The previous results are kept under previous/."""
        run = self._runs.get(study_id)
        if run is not None and run.analysing.is_set():
            raise RigError(f"{study_id} is still {run.study['status']}")
        record = self.studies.load(study_id)
        if record.get("status") in ("recording", "analysing"):
            raise RigError(f"{study_id} is {record['status']}")
        entries = record["positions"] + ([record["check"]] if record.get("check") else [])
        if not any(e.get("bracket") for e in entries):
            raise RigError(f"{study_id} has no recorded brackets to analyse")
        cam_name = (record.get("camera_profile") or {}).get("name")
        if cam_name not in self.camera_profiles.names():
            cam_name = None
        if not self.processing_setup(cam_name)[0]:
            raise RigError("no calibration constant k: calibrate first, then re-analyse")
        run = StudyRun(self, record)
        run.prepare_reanalysis()
        self._runs[study_id] = run
        threading.Thread(target=run.analyse, name=f"reanalysis-{study_id}", daemon=True).start()
        return run.study

    def study(self, study_id: str) -> dict[str, Any]:
        run = self._runs.get(study_id)
        return dict(run.study) if run is not None else self.studies.load(study_id)

    def list_studies(self, q: str = "") -> list[dict[str, Any]]:
        return self.index.search(q)

    def _recover_studies(self) -> None:
        """A study left recording or analysing by a server that stopped is interrupted:
        say so in its record, so it is never mistaken for a finished one."""
        for sid in self.studies.ids():
            try:
                st = self.studies.load(sid)
            except (KeyError, ValueError, OSError):
                continue
            if st.get("status") in ("recording", "analysing"):
                st["status"] = "interrupted"
                st.setdefault("flags", []).append(
                    "interrupted: the rig server stopped during this study")
                self.studies.save(st)

    # -- status ------------------------------------------------------------- #

    def status(self) -> dict[str, Any]:
        with self._lock:
            m, last = self._motion, self._last
            ping_age = time.monotonic() - self._last_ping if self._last_ping else None
        st = self.mount.status()
        st.update({
            "state": "idle" if m is None else m.kind,
            "speed": self._speed,
            "motion": None if m is None else m.summary(),
            "last_motion": None if last is None else last.summary(),
            "jog_ping_age_s": None if m is None or m.kind != "jog" or ping_age is None
            else round(ping_age, 3),
            "last_known": self._last_known,
            "limits": {a.name: [a.min_deg, a.max_deg]
                       for a in (self.mount.cfg.pan, self.mount.cfg.tilt)},
            "axes": {a.name: {"min_deg": a.min_deg, "max_deg": a.max_deg,
                              "max_speed_deg_s": a.max_speed_deg_s,
                              "accel_deg_s2": a.accel_deg_s2, "backlash_deg": a.backlash_deg,
                              "approach_sign": a.approach_sign}
                     for a in (self.mount.cfg.pan, self.mount.cfg.tilt)},
            "unzeroed_jog_deg": self.mount.cfg.unzeroed_jog_deg,
            "zero": self.zero_info(),
            "zero_check": self._last_check if self.mount.is_zeroed else None,
            "live_view": self.live.status(),
            "studies_active": {sid: r.study["status"] for sid, r in list(self._runs.items())
                               if r.analysing.is_set()},
            "disk": _disk(self.data_dir),
        })
        return st

    # -- power and settings ------------------------------------------------- #

    def enable(self) -> dict[str, Any]:
        with self._lock:
            self._require_idle()
            self.mount.enable()
        return self.status()

    def disable(self) -> dict[str, Any]:
        """Release the drivers. The head can then be turned by hand, so this drops the datum."""
        with self._lock:
            self._require_idle()
            self.mount.disable()
            self._write_state()
        return self.status()

    def set_speed(self, fraction: float) -> dict[str, Any]:
        if not 0.0 < fraction <= 1.0:
            raise ValueError("speed is a fraction of the configured maximum, 0 < speed <= 1")
        self._speed = float(fraction)
        return self.status()

    # -- motion ------------------------------------------------------------- #

    def jog_start(self, direction: str, speed: float | None = None) -> Motion:
        axis, sign = _direction(direction)
        spd = self._spd(speed)
        with self._lock:
            m = self._motion
            if (m is not None and m.kind == "jog" and m.detail.get("direction") == direction
                    and not m.stop.is_set()):
                self._last_ping = time.monotonic()  # repeated press: same jog, keep it alive
                return m
            self._require_idle()
            self._require_enabled()
            self._last_ping = time.monotonic()
            return self._start("jog", {"direction": direction, "speed": spd},
                               lambda stop: _move(self.mount.jog(axis, sign, spd, stop)))

    def jog_ping(self) -> bool:
        """Keep a running jog alive. False when no jog is running (it already stopped)."""
        with self._lock:
            m = self._motion
            if m is None or m.kind != "jog" or m.stop.is_set():
                return False
            self._last_ping = time.monotonic()
            return True

    def jog_stop(self) -> Motion | None:
        with self._lock:
            m = self._motion
            if m is not None and m.kind == "jog":
                self._request_stop(m, "released")
            return m

    def nudge(self, direction: str, deg: float, speed: float | None = None) -> Motion:
        if not deg > 0:
            raise ValueError("nudge size must be positive")
        axis, sign = _direction(direction)
        spd = self._spd(speed)
        pan, tilt = (sign * deg, 0.0) if axis == "pan" else (0.0, sign * deg)
        with self._lock:
            self._require_idle()
            self._require_enabled()
            self.mount.check_nudge(pan, tilt)
            return self._start("nudge", {"direction": direction, "deg": deg, "speed": spd},
                               lambda stop: _move(self.mount.nudge(pan, tilt, spd, stop)))

    def goto(self, pan_deg: float, tilt_deg: float, speed: float | None = None) -> Motion:
        spd = self._spd(speed)
        with self._lock:
            self._require_idle()
            self._require_enabled()
            self._require_zero()
            self.mount.check_target(pan_deg, tilt_deg)
            return self._start("goto", {"pan_deg": pan_deg, "tilt_deg": tilt_deg, "speed": spd},
                               lambda stop: _move(self.mount.move_to(pan_deg, tilt_deg,
                                                                     speed=spd, stop=stop)))

    def set_zero(self) -> Motion:
        """"Save home": take the backlash up along the approach direction, then declare this
        pose (0, 0). Without the take-up the datum would sit a backlash away from where every
        later absolute move arrives."""
        with self._lock:
            self._require_idle()
            self._require_enabled()

            def run(stop: threading.Event) -> dict[str, Any]:
                moved = self.mount.take_up_backlash(self._speed, stop)
                if stop.is_set():
                    raise RigError("zero cancelled before the backlash was taken up")
                self._require_lash("set the zero")
                self.mount.set_zero()
                self._zero_info = {"method": "manual", "at": now_iso()}
                self._last_check = None
                return {"backlash_taken_up": moved, "pan_deg": 0.0, "tilt_deg": 0.0}

            return self._start("zero", {}, run)

    # -- optical zero, zero check, lens geometry ----------------------------- #

    def _frame_geometry(self, shape: tuple[int, int], focal_mm: float | None = None,
                        k1: float | None = None):
        from photometry.profile import load_analysis_config
        from pipeline import optical

        s = load_analysis_config().sensor
        p = self.camera_profile()
        return optical.frame_geometry(shape, s.pixel_pitch_um * 1e-3 * s.width_px,
                                      p.focal_mm if focal_mm is None else focal_mm,
                                      p.distortion_k1 if k1 is None else k1)

    def live_frames(self, cam, n: int) -> list[bytes]:
        """`n` live-view frames from a camera the caller holds (inside `live.paused()`), after
        one thrown away: the first frame after a move can be one the body buffered before
        it. Live view is ended again, so nothing stays warm."""
        try:
            cam.preview()
            return [cam.preview() for _ in range(n)]
        finally:
            cam.end_preview()

    def measure_fiducial(self, cam, expect: tuple[float, float] = (0.0, 0.0)):
        """The fiducial seen from where the head is now (pipeline.optical.measure_fiducial).
        Raises analysis.spot.SpotNotFound when it is not near `expect`."""
        from analysis.spot import gray
        from pipeline import optical

        cfg = optical.load_optical_config()
        frames = self.live_frames(cam, cfg.frames)
        geom = self._frame_geometry(gray(frames[0]).shape)
        return optical.measure_fiducial(frames, geom, self.mount.position(), cfg, expect)

    def zero_info(self) -> dict[str, Any] | None:
        return self._zero_info if self.mount.is_zeroed else None

    def note_zero(self, method: str, m=None) -> None:
        self._zero_info = {"method": method, "at": now_iso()}
        if m is not None:
            self._zero_info["correction_deg"] = [round(m.pan_deg, 4), round(m.tilt_deg, 4)]
        self._last_check = None

    def note_check(self, m) -> None:
        from pipeline.optical import load_optical_config

        self._last_check = {"at": now_iso(), "offset_deg": round(m.offset_deg, 4),
                            "limit_deg": load_optical_config().drift_limit_deg}

    def optical_zero(self) -> Motion:
        """Zero on the fiducial. Aim roughly at it first (within [optical] search_deg of the
        crosshair). The backlash is taken up, the fiducial is centroided, and the datum is
        set so that the fiducial is (0, 0); then the head goes to (0, 0) and measures again,
        which is the residual. Works with or without a zero: without one, the current pose
        is the first guess."""
        from pipeline import optical

        with self._lock:
            self._require_idle()
            self._require_enabled()
            if self.live.camera is None:
                raise RigError("no camera: optical zeroing needs live view")

            def run(stop: threading.Event) -> dict[str, Any]:
                self.mount.take_up_backlash(self._speed, stop)
                if stop.is_set():
                    raise RigError("zero cancelled before the backlash was taken up")
                self._require_lash("set the zero")
                if not self.mount.is_zeroed:
                    self.mount.set_zero()
                pan, tilt = self.mount.position()
                with self.live.paused() as cam:
                    m = self.measure_fiducial(cam, expect=(pan, tilt))
                    before = self._zero_info
                    self.mount.set_zero(pan - m.pan_deg, tilt - m.tilt_deg)
                    r = self.mount.move_to(0.0, 0.0, speed=self._speed, stop=stop)
                    if r.stopped:
                        raise RigError("stopped before the check at (0, 0)")
                    check = self.measure_fiducial(cam)
                info = {"method": "optical", "at": now_iso(),
                        "correction_deg": [round(m.pan_deg - pan, 4), round(m.tilt_deg - tilt, 4)],
                        "residual_deg": round(check.offset_deg, 4),
                        "spread_px": round(max(m.spread_px, check.spread_px), 3),
                        "previous": before}
                self._zero_info = {k: v for k, v in info.items() if k != "previous"}
                self._last_check = {"at": info["at"], "offset_deg": info["residual_deg"],
                                    "limit_deg": optical.load_optical_config().drift_limit_deg}
                return {**info, "fiducial": m.as_dict(), "check": check.as_dict()}

            return self._start("optical_zero", {}, run)

    def check_zero(self) -> Motion:
        """Go to (0, 0) and measure the fiducial without correcting anything: how far the
        head has drifted since the zero (the drift check, any time)."""
        from pipeline.optical import load_optical_config

        with self._lock:
            self._require_idle()
            self._require_enabled()
            if self.live.camera is None:
                raise RigError("no camera: the zero check needs live view")
            if not self.mount.is_zeroed:
                raise RigError("no zero to check")

            def run(stop: threading.Event) -> dict[str, Any]:
                r = self.mount.move_to(0.0, 0.0, speed=self._speed, stop=stop)
                if r.stopped:
                    raise RigError("stopped before (0, 0)")
                with self.live.paused() as cam:
                    m = self.measure_fiducial(cam)
                limit = load_optical_config().drift_limit_deg
                out = {"at": now_iso(), "offset_deg": round(m.offset_deg, 4),
                       "pan_deg": round(m.pan_deg, 4), "tilt_deg": round(m.tilt_deg, 4),
                       "limit_deg": limit, "over_limit": m.offset_deg > limit,
                       "zero": self._zero_info, "fiducial": m.as_dict()}
                self._last_check = {"at": out["at"], "offset_deg": out["offset_deg"],
                                    "limit_deg": limit}
                return out

            return self._start("check_zero", {}, run)

    def measure_lens(self, profile: str, distance_m: float) -> Motion:
        """Track the fiducial over a grid of poses around the zero and fit the lens's focal
        length and k1 (photometry.lensfit). The fit is offered, not applied: accept it into
        the camera profile (`accept_lens`). The fiducial must be at (0, 0): zero on it first."""
        from photometry import lensfit
        from photometry.profile import load_analysis_config
        from pipeline import optical

        prof = self.camera_profiles.load(profile)
        if not distance_m > 0:
            raise ValueError("the distance to the fiducial must be positive (metres)")
        with self._lock:
            self._require_idle()
            self._require_enabled()
            if self.live.camera is None:
                raise RigError("no camera: the lens measurement needs live view")
            if not self.mount.is_zeroed:
                raise RigError("zero on the fiducial first: the lens is measured around it")
            fid = f"{now_iso().replace(':', '').replace('-', '')}-{slug(prof.name)}"
            ocfg = optical.load_optical_config()
            sensor = load_analysis_config().sensor
            sensor_w = sensor.pixel_pitch_um * 1e-3 * sensor.width_px
            nominal = self._frame_geometry((2, 3), prof.focal_mm, prof.distortion_k1)
            fov = tuple(float(x) for x in nominal.fov_deg())
            poses = lensfit.grid(fov)
            limits = self.status()["limits"]
            poses = [(p, t) for p, t in poses if limits["pan"][0] <= p <= limits["pan"][1]
                     and limits["tilt"][0] <= t <= limits["tilt"][1]]

            def run(stop: threading.Event) -> dict[str, Any]:
                from analysis.spot import SpotNotFound, gray

                sightings, missed, shape = [], [], None
                with self.live.paused() as cam:
                    for i, (p, t) in enumerate(poses):
                        if stop.is_set():
                            raise RigError("lens measurement stopped")
                        self._motion.detail["progress"] = f"{i + 1} of {len(poses)}"
                        r = self.mount.move_to(p, t, speed=self._speed, stop=stop)
                        if r.stopped:
                            raise RigError("lens measurement stopped")
                        frames = self.live_frames(cam, ocfg.frames)
                        shape = gray(frames[0]).shape
                        geom = optical.frame_geometry(shape, sensor_w, prof.focal_mm,
                                                      prof.distortion_k1)
                        try:
                            m = optical.measure_fiducial(frames, geom, (p, t), ocfg)
                        except SpotNotFound as exc:
                            missed.append({"pan_deg": p, "tilt_deg": t, "why": str(exc)})
                            continue
                        sightings.append(lensfit.Sighting(p, t, m.u, m.v))
                    self.mount.move_to(0.0, 0.0, speed=self._speed, stop=stop)
                if len(sightings) < 12:
                    raise RigError(f"the fiducial was found at {len(sightings)} of "
                                   f"{len(poses)} poses: need 12 (is it at (0, 0)?)")
                f = lensfit.fit(sightings, shape[1], shape[0], sensor_w, prof.focal_mm,
                                distance_m, ocfg.pupil_offset_mm)
                notes = []
                if ocfg.pupil_offset_mm == 0:
                    notes.append("pupil offset not measured (mount.toml [optical]): parallax "
                                 f"not corrected, focal length biased by up to ~e/D at "
                                 f"{distance_m:g} m")
                notes.append("rests on the gear ratios (PIVOT_DETAIL §8) and on live view "
                             "showing the whole sensor width (REQUIREMENTS §9)")
                record = {"id": fid, "at": now_iso(), "profile": prof.name,
                          "nominal": {"focal_mm": prof.focal_mm, "k1": prof.distortion_k1},
                          "distance_m": distance_m, "fit": f.as_dict(), "missed": missed,
                          "notes": notes, "accepted_at": None}
                d = self.lensfits_dir / fid
                d.mkdir(parents=True, exist_ok=True)
                (d / "lensfit.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
                return record

            return self._start("lens", {"profile": prof.name, "lensfit": fid,
                                         "progress": f"0 of {len(poses)}"}, run)

    def lens_fits(self, profile: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        out = []
        if self.lensfits_dir.is_dir():
            for f in sorted(self.lensfits_dir.glob("*/lensfit.json"), reverse=True):
                rec = json.loads(f.read_text(encoding="utf-8"))
                if profile is None or rec["profile"] == profile:
                    out.append(rec)
                if len(out) >= limit:
                    break
        return out

    def accept_lens(self, profile: str, lensfit_id: str) -> CameraProfile:
        """Make a measured focal length and k1 the profile's."""
        f = self.lensfits_dir / lensfit_id / "lensfit.json"
        if "/" in lensfit_id or "\\" in lensfit_id or not f.is_file():
            raise ProfileNotFound(f"no lens measurement {lensfit_id!r}")
        rec = json.loads(f.read_text(encoding="utf-8"))
        if rec["profile"] != profile:
            raise RigError(f"lens measurement {lensfit_id} was made with profile {rec['profile']}")
        with self._profiles_lock:
            p = self.camera_profiles.load(profile).with_lens(
                rec["fit"]["focal_mm"], rec["fit"]["k1"], rec["at"], lensfit_id)
            self.camera_profiles.save(p)
        rec["accepted_at"] = now_iso()
        f.write_text(json.dumps(rec, indent=2), encoding="utf-8")
        return p

    def take_up_backlash(self) -> Motion:
        with self._lock:
            self._require_idle()
            self._require_enabled()
            return self._start("backlash", {}, lambda stop: {
                "moved": self.mount.take_up_backlash(self._speed, stop),
                **_angles(self.mount)})

    def stop(self) -> Motion | None:
        """Controlled stop of whatever is running. The position stays exact."""
        with self._lock:
            m = self._motion
            if m is not None:
                self._request_stop(m, "stop")
            return m

    def estop(self) -> Motion | None:
        """Cut the step stream at once. The position becomes an estimate: re-zero."""
        with self._lock:
            m = self._motion
            if m is not None:
                self._request_stop(m, "emergency stop")
        self.mount.abort()
        return m

    def wait(self, motion: Motion, timeout: float | None = None) -> dict[str, Any]:
        """Block until `motion` ends; re-raise its error, else return its summary."""
        if not motion.done.wait(timeout):
            return motion.summary()
        if motion.error is not None:
            raise motion.error
        return motion.summary()

    # -- position profiles -------------------------------------------------- #

    def list_profiles(self) -> list[dict[str, Any]]:
        with self._profiles_lock:
            out = []
            for name in self.profiles.names():
                p = self.profiles.load(name)
                out.append({"name": p.name, "vehicle": p.vehicle, "positions": len(p.positions),
                            "updated_at": p.updated_at})
            return out

    def get_profile(self, name: str) -> PositionProfile:
        with self._profiles_lock:
            return self.profiles.load(name)

    def coverage(self, profile: str, png: bool = False) -> dict[str, Any] | bytes:
        """Glare-weighted coverage of the forward field by a profile's positions, and a
        tiling that would bring it to [forward] coverage_min (analysis.forward). With `png`,
        the map instead."""
        from analysis import forward
        from photometry.profile import load_analysis_config

        p = self.get_profile(profile)
        if p.forward_gaze is None:
            raise RigError("set the forward gaze first: coverage is measured around it")
        cfg = load_analysis_config()
        fov = tuple(float(x) for x in self.geometry["fov_deg"])
        cap = cfg.ugr.position_index_cap
        frames = [(q.name, q.pan_deg, q.tilt_deg) for q in p.positions]
        prop = forward.propose_tiling(p.forward_gaze, fov, self.status()["limits"], cap,
                                      cfg.forward.coverage_min, cfg.forward.tiling_overlap,
                                      existing=[(a, b) for _, a, b in frames])
        if png:
            return forward.coverage_png(p.forward_gaze, fov, frames, cap,
                                        proposed=[tuple(f) for f in prop["frames"]],
                                        title=p.name)
        grid = forward.hemisphere(p.forward_gaze, cap, cfg.forward.coverage_bin_deg)
        cov = forward.coverage(grid, forward.covered(grid, [(a, b) for _, a, b in frames], fov))
        return {"profile": p.name, "gaze": list(p.forward_gaze), "coverage": cov,
                "coverage_min": cfg.forward.coverage_min, "fov_deg": list(fov),
                "proposal": prop}

    def describe(self, p: PositionProfile) -> dict[str, Any]:
        """The profile as the API returns it: the TOML content plus, per position, whether
        it has a reference image and how many masks."""
        d = p.as_dict()
        with self._profiles_lock:
            for pos, out in zip(p.positions, d["positions"]):
                out["has_image"] = self.profiles.image_path(p.name, pos).is_file()
                out["masks"] = mask_counts(self.profiles.load_masks(p.name, pos))
        return d

    def create_profile(self, name: str, vehicle: str = "",
                       zero_reference: str = "") -> PositionProfile:
        with self._profiles_lock:
            return self.profiles.create(name, vehicle, zero_reference)

    def update_profile(self, name: str, vehicle: str | None = None,
                       zero_reference: str | None = None) -> PositionProfile:
        with self._profiles_lock:
            p = self.profiles.load(name).with_details(vehicle, zero_reference)
            self.profiles.save(p)
            return p

    def delete_profile(self, name: str) -> str:
        with self._profiles_lock:
            return str(self.profiles.delete(name))

    def save_position(self, profile: str, name: str) -> Motion:
        """Record the current pose as a named position. Takes the backlash up first: a jog
        may have arrived from either side, but replay always arrives along approach_sign,
        so without this the position would replay a backlash away from what was seen."""
        self.get_profile(profile)  # 404 now, not from the worker thread
        name = check_name(name, "position")
        with self._lock:
            self._require_idle()
            self._require_enabled()
            self._require_zero()

            def run(stop: threading.Event) -> dict[str, Any]:
                moved = self.mount.take_up_backlash(self._speed, stop)
                if stop.is_set():
                    raise RigError("save cancelled before the backlash was taken up")
                self._require_lash("save a position")
                pan, tilt = self.mount.position()
                pos = Position(name, round(pan, 4), round(tilt, 4), now_iso())
                # The reference image is what this position looks like, taken now that the
                # backlash is on the replay side: the mask editor draws on it.
                jpeg = self.live.snapshot(timeout=3.0)
                with self._profiles_lock:
                    p = self.profiles.load(profile).with_position(pos)
                    self.profiles.save(p)
                    saved = p.position(name)
                    if jpeg:
                        self.profiles.save_image(p.name, saved, jpeg)
                return {"position": saved.as_dict(), "backlash_taken_up": moved,
                        "reference_image": bool(jpeg), "profile_hash": p.content_hash}

            return self._start("save", {"profile": profile, "position": name}, run)

    def delete_position(self, profile: str, name: str) -> PositionProfile:
        """Remove a position; its image and masks go to the trash with it."""
        with self._profiles_lock:
            p0 = self.profiles.load(profile)
            pos = p0.position(name)
            p = p0.without_position(name)
            self.profiles.save(p)
            self.profiles.trash_position_files(p.name, pos)
            return p

    # -- reference images and masks ------------------------------------------ #

    def position_image(self, profile: str, name: str) -> bytes | None:
        with self._profiles_lock:
            p = self.profiles.load(profile)
            path = self.profiles.image_path(p.name, p.position(name))
            return path.read_bytes() if path.is_file() else None

    def retake_image(self, profile: str, name: str) -> PositionProfile:
        """Replace a position's reference image with live view now. The head must be at the
        position, or the image would not match the angles the masks are drawn for."""
        p = self.get_profile(profile)
        pos = p.position(name)
        with self._lock:
            self._require_idle()
            self._require_zero()
            pan, tilt = self.mount.position()
        if abs(pan - pos.pan_deg) > HERE_DEG or abs(tilt - pos.tilt_deg) > HERE_DEG:
            raise RigError(f"go to {name!r} first: the head is at {pan:+.3f}, {tilt:+.3f}, "
                           f"the position at {pos.pan_deg:+.3f}, {pos.tilt_deg:+.3f}")
        jpeg = self.live.snapshot(timeout=3.0)
        if not jpeg:
            raise RigError("no live-view frame: "
                           + (self.live.status()["error"] or f"camera {self.live.source}"))
        with self._profiles_lock:
            self.profiles.save_image(p.name, pos, jpeg)
        return p

    def position_masks(self, profile: str, name: str) -> dict[str, Any]:
        with self._profiles_lock:
            p = self.profiles.load(profile)
            pos = p.position(name)
            return (self.profiles.load_masks(p.name, pos)
                    or {"version": None, "exclude": [], "glazing": [], "include": []})

    def set_position_masks(self, profile: str, name: str,
                           masks: dict[str, Any]) -> dict[str, Any]:
        with self._profiles_lock:
            p = self.profiles.load(profile)
            return self.profiles.save_masks(p.name, p.position(name), masks)

    def reorder_positions(self, profile: str, names: list[str]) -> PositionProfile:
        with self._profiles_lock:
            p = self.profiles.load(profile).reordered(names)
            self.profiles.save(p)
            return p

    def set_forward_gaze(self, profile: str) -> PositionProfile:
        """Record where the camera points now as the driver's forward gaze for this profile
        (the line of sight of the study's headline UGR)."""
        with self._lock:
            self._require_idle()
            self._require_zero()
            pan, tilt = self.mount.position()
        with self._profiles_lock:
            p = self.profiles.load(profile).with_gaze(round(pan, 4), round(tilt, 4))
            self.profiles.save(p)
            return p

    def goto_position(self, profile: str, name: str, speed: float | None = None) -> Motion:
        pos = self.get_profile(profile).position(name)
        spd = self._spd(speed)
        with self._lock:
            self._require_idle()
            self._require_enabled()
            self._require_zero()
            return self._start(
                "goto", {"profile": profile, "position": name, "pan_deg": pos.pan_deg,
                         "tilt_deg": pos.tilt_deg, "speed": spd},
                lambda stop: _move(self.mount.move_to(pos.pan_deg, pos.tilt_deg,
                                                      speed=spd, stop=stop)))

    def run_profile(self, profile: str, dwell_s: float = 0.0,
                    speed: float | None = None) -> Motion:
        """Visit every position in order. Capture joins this sequence with the session
        pipeline; today it checks that a list replays."""
        p = self.get_profile(profile)
        if not p.positions:
            raise RigError(f"profile {profile!r} has no positions")
        if dwell_s < 0:
            raise ValueError("dwell must not be negative")
        spd = self._spd(speed)
        with self._lock:
            self._require_idle()
            self._require_enabled()
            self._require_zero()
            detail: dict[str, Any] = {"profile": profile, "of": len(p.positions), "index": 0,
                                      "speed": spd}

            def run(stop: threading.Event) -> dict[str, Any]:
                visited = []
                for i, pos in enumerate(p.positions):
                    if stop.is_set():
                        break
                    detail.update(index=i + 1, position=pos.name)
                    r = self.mount.move_to(pos.pan_deg, pos.tilt_deg, speed=spd, stop=stop)
                    visited.append({"name": pos.name, **_move(r)})
                    if r.stopped or stop.wait(dwell_s):
                        break
                return {"visited": visited, "complete": len(visited) == len(p.positions)
                        and not any(v["stopped"] for v in visited)}

            return self._start("run", detail, run)

    # -- internals ---------------------------------------------------------- #

    def _spd(self, speed: float | None) -> float:
        spd = self._speed if speed is None else float(speed)
        if not 0.0 < spd <= 1.0:
            raise ValueError("speed is a fraction of the configured maximum, 0 < speed <= 1")
        return spd

    def _require_idle(self) -> None:
        m = self._motion
        if m is not None:
            raise RigBusy(f"{m.kind} in progress - stop it first")

    def _require_enabled(self) -> None:
        if not self.mount.enabled:
            raise MountError("drivers are not enabled - enable them first")

    def _require_lash(self, what: str) -> None:
        bad = [a for a, ok in self.mount.status()["backlash_ok"].items() if not ok]
        if bad:
            raise RigError(f"cannot {what}: no room to take up {' and '.join(bad)} backlash "
                           "this close to the soft limit - move away from it and try again")

    def _require_zero(self) -> None:
        if not self.mount.is_zeroed:
            raise NotZeroed("no zero set - set zero (save home) first")

    def _request_stop(self, m: Motion, reason: str) -> None:
        if not m.stop.is_set():
            m.stop_reason = reason
            m.stop.set()

    def _start(self, kind: str, detail: dict[str, Any],
               fn: Callable[[threading.Event], dict[str, Any]]) -> Motion:
        """Run `fn(stop)` in a worker thread as the current motion. Caller holds _lock and
        has checked that the rig is idle."""
        m = Motion(self._next_id, kind, detail)
        self._next_id += 1

        def body() -> None:
            try:
                m.result = fn(m.stop)
            except Exception as exc:  # noqa: BLE001 - reported through status, re-raised by wait()
                m.error = exc
                log.warning("%s failed: %s: %s", kind, type(exc).__name__, exc)
            finally:
                m.ended_at = time.monotonic()
                with self._lock:
                    self._motion = None
                    self._last = m
                self._write_state()
                m.done.set()

        self._motion = m
        threading.Thread(target=body, name=f"rig-{kind}-{m.id}", daemon=True).start()
        return m

    def _watch(self) -> None:
        while not self._closing.wait(WATCHDOG_PERIOD_S):
            with self._lock:
                m = self._motion
                if (m is not None and m.kind == "jog" and not m.stop.is_set()
                        and time.monotonic() - self._last_ping > HEARTBEAT_TIMEOUT_S):
                    log.warning("jog heartbeat lost - ramping down")
                    self._request_stop(m, "heartbeat lost")

    def _read_state(self) -> dict[str, Any] | None:
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        raw["note"] = "last position before the service stopped; a hint only - re-zero"
        return raw

    def _write_state(self) -> None:
        """Persist the last position as a restart hint. Never trusted on its own: once the
        drivers are released the head can be turned by hand, or sag."""
        st = self.mount.status()
        state = {k: st[k] for k in ("pan_deg", "tilt_deg", "zeroed", "certain", "enabled")}
        state["at"] = now_iso()
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
            os.replace(tmp, self._state_path)
        except OSError as exc:
            log.warning("could not write %s: %s", self._state_path, exc)


def software_version() -> dict[str, Any]:
    """Git commit of the running code, for provenance: a code change can change a number."""
    import subprocess

    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True,
                              timeout=5, check=True).stdout.strip()
    try:
        return {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain"))}
    except (OSError, subprocess.SubprocessError):
        return {"commit": None, "dirty": None}


def _disk(path: Path) -> dict[str, float] | None:
    import shutil
    try:
        u = shutil.disk_usage(path if path.exists() else path.parent)
    except OSError:
        return None
    return {"free_gb": round(u.free / 1e9, 1), "total_gb": round(u.total / 1e9, 1)}


def camera_geometry(focal_mm: float | None = None, k1: float | None = None) -> dict[str, Any]:
    """Field of view and lens, for live-view overlays: config/analysis.toml, with a camera
    profile's focal length and distortion when given. Nominal until the lens is measured."""
    import dataclasses

    from photometry.masks import CameraGeometry
    from photometry.profile import load_analysis_config

    cfg = load_analysis_config()
    if focal_mm is not None:
        cfg = dataclasses.replace(cfg, lens=dataclasses.replace(
            cfg.lens, focal_mm=focal_mm, distortion_k1=cfg.lens.distortion_k1 if k1 is None else k1))
    b = cfg.processing.binning
    g = CameraGeometry.from_config(cfg, (cfg.sensor.height_px // b, cfg.sensor.width_px // b))
    fh, fv = g.fov_deg()
    return {"fov_deg": [round(float(fh), 4), round(float(fv), 4)],
            "focal_mm": cfg.lens.focal_mm, "lens": cfg.lens.name, "k1": cfg.lens.distortion_k1,
            "width_px": g.width, "height_px": g.height}


def sim_pose(sim: SimBackend, cfg: MountConfig) -> Callable[[], tuple[float, float]]:
    """The simulated head's physical (pan, tilt) relative to power-up, from the signed step
    counts. Unlike mount.position() it ignores the datum, as the world does."""
    sim.pair(cfg.pins.pan_step, cfg.pins.pan_dir)
    sim.pair(cfg.pins.tilt_step, cfg.pins.tilt_dir)

    def pose() -> tuple[float, float]:
        out = []
        for axis, pin in ((cfg.pan, cfg.pins.pan_step), (cfg.tilt, cfg.pins.tilt_step)):
            sign = -1 if axis.invert else 1
            out.append(sign * sim.net_steps.get(pin, 0) / axis.steps_per_deg)
        return out[0], out[1]

    return pose


def _direction(direction: str) -> tuple[str, int]:
    try:
        return DIRECTIONS[direction]
    except KeyError:
        raise ValueError(f"direction must be one of {sorted(DIRECTIONS)}") from None


def _move(r: MoveResult) -> dict[str, Any]:
    d = asdict(r)
    d["pan_deg"] = round(r.pan_deg, 4)
    d["tilt_deg"] = round(r.tilt_deg, 4)
    d["motion_s"] = round(r.motion_s, 3)
    return d


def _angles(mount: PanTiltMount) -> dict[str, float]:
    pan, tilt = mount.position()
    return {"pan_deg": round(pan, 4), "tilt_deg": round(tilt, 4)}


__all__ = ["RigController", "RigError", "RigBusy", "Motion", "DIRECTIONS", "MountError",
           "HEARTBEAT_TIMEOUT_S"]
