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

from .liveview import LiveView
from .positions import Position, PositionProfile, ProfileStore, check_name, now_iso

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
        self.geometry = geometry or camera_geometry()
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
            cam = FakeCamera(load_camera_config(), pose=sim_pose(pulses, cfg),
                             view=CabView(tuple(geometry["fov_deg"])))
        elif camera != "none":
            raise ValueError(f"unknown camera {camera!r}: gphoto2, sim or none")
        live = LiveView(cam, camera)
        return cls(PanTiltMount(cfg, pulses), data_dir, live, geometry)

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
            "live_view": self.live.status(),
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
                return {"backlash_taken_up": moved, "pan_deg": 0.0, "tilt_deg": 0.0}

            return self._start("zero", {}, run)

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

    def create_profile(self, name: str, vehicle: str = "",
                       zero_reference: str = "") -> PositionProfile:
        with self._profiles_lock:
            return self.profiles.create(name, vehicle, zero_reference)

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
                with self._profiles_lock:
                    p = self.profiles.load(profile).with_position(pos)
                    self.profiles.save(p)
                return {"position": pos.as_dict(), "backlash_taken_up": moved,
                        "profile_hash": p.content_hash}

            return self._start("save", {"profile": profile, "position": name}, run)

    def delete_position(self, profile: str, name: str) -> PositionProfile:
        with self._profiles_lock:
            p = self.profiles.load(profile).without_position(name)
            self.profiles.save(p)
            return p

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


def camera_geometry() -> dict[str, Any]:
    """Field of view and lens from config/analysis.toml, for live-view overlays. Nominal: the
    focal length is a MEASURE placeholder until measured."""
    from photometry.masks import CameraGeometry
    from photometry.profile import load_analysis_config

    cfg = load_analysis_config()
    b = cfg.processing.binning
    g = CameraGeometry.from_config(cfg, (cfg.sensor.height_px // b, cfg.sensor.width_px // b))
    fh, fv = g.fov_deg()
    return {"fov_deg": [round(float(fh), 4), round(float(fv), 4)],
            "focal_mm": cfg.lens.focal_mm, "lens": cfg.lens.name,
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
