"""Pan-tilt head driver.

Hardware-only. Optical zeroing needs the camera as well and belongs in
`pipeline/`; this module must stay runnable (against the sim backend) with
nothing attached.

Position is held as signed steps from the zero datum. Degrees are a derived
view, so no rounding error accumulates over a session.

Backlash is tracked per axis as the direction of the last motion. A target is
only repeatable when it was reached moving along `approach_sign`, so anything
that may have left an axis the other way (a jog, a nudge, an abort, power-up)
is remembered, and the next absolute move or `take_up_backlash()` corrects it.

Stopping comes in two kinds. `stop()` is a planned ramp-down: the step count
stays exact and the position stays certain. `abort()` cuts the DMA stream at
once: the position becomes an estimate.
"""

from __future__ import annotations

import atexit
import logging
import signal
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from .backend import PulseBackend, SimBackend, create_backend
from .config import AxisConfig, MountConfig, load_config
from .motion import AxisPlan, WaveChunk, build_chunks, plan_timeline, stoppable

log = logging.getLogger(__name__)

MAX_WAVES_IN_FLIGHT = 3
POLL_S = 0.005
# Longest wave. With three in flight a stop starts ramping within ~0.3 s. At the
# head's speeds (a few hundred microsteps/s) a late wave costs a pause, not a
# lost step, so short waves are safe.
CHUNK_US = 100_000


class MountError(RuntimeError):
    pass


class NotZeroed(MountError):
    """move_to() before a datum exists. Power-up position is not 0."""


class SoftLimit(MountError):
    pass


class MoveAborted(MountError):
    """abort() was called mid-move. Position is an estimate; re-zero."""


@dataclass
class _InFlight:
    wave_id: int
    pan_steps: int
    tilt_steps: int
    duration_s: float
    queued_at: float
    started_at: float | None = None  # when it was first seen transmitting


@dataclass(frozen=True)
class MoveResult:
    pan_deg: float
    tilt_deg: float
    motion_s: float
    settle_s: float
    backlash_stage: bool
    stopped: bool = False  # stop() ended it short of the target; position is still exact


class PanTiltMount:
    def __init__(self, config: MountConfig, backend: PulseBackend, drivers=None) -> None:
        """`drivers`: a `hal.tmc2209.DriverBus`. Required with real hardware: without the
        UART configuration the drivers run 1/8 and 1/32 microstepping and every angle is
        wrong, so `enable()` refuses. The pulse simulator runs with or without one."""
        self.cfg = config
        self._b = backend
        self.drivers = drivers
        self.driver_warnings: list[str] = []
        self._pan_steps = 0
        self._tilt_steps = 0
        self._zeroed = False
        self._certain = True
        self._enabled = False
        self._abort = threading.Event()
        self._stop: threading.Event | None = None  # the running motion's stop request
        self._lash: dict[str, int | None] = {"pan": None, "tilt": None}  # last direction
        self._lock = threading.RLock()
        self._closed = False

        pins = config.pins
        off = 1 if pins.enable_active_low else 0
        self._b.set_output(pins.enable, off)
        for gpio in (pins.pan_step, pins.pan_dir, pins.tilt_step, pins.tilt_dir):
            self._b.set_output(gpio, 0)
        self._b.wave_clear()
        self._max_pulses = self._b.wave_get_max_pulses()
        atexit.register(self._safe_shutdown)

    @classmethod
    def from_config(
        cls,
        path: str | Path | None = None,
        backend: str = "pigpio",
        **backend_kwargs,
    ) -> "PanTiltMount":
        cfg = load_config(path)
        from .tmc2209 import create_drivers

        drivers = create_drivers("uart" if backend == "pigpio" else "sim", cfg.drivers,
                                 (cfg.pan, cfg.tilt))
        return cls(cfg, create_backend(backend, **backend_kwargs), drivers)

    # -- state ------------------------------------------------------------ #

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def is_zeroed(self) -> bool:
        return self._zeroed

    @property
    def position_certain(self) -> bool:
        """False after an abort. Every angle since is suspect until re-zeroed."""
        return self._certain

    def position(self) -> tuple[float, float]:
        return (
            self.cfg.pan.steps_to_deg(self._pan_steps),
            self.cfg.tilt.steps_to_deg(self._tilt_steps),
        )

    def position_steps(self) -> tuple[int, int]:
        return self._pan_steps, self._tilt_steps

    def status(self) -> dict:
        pan, tilt = self.position()
        return {
            "enabled": self._enabled,
            "zeroed": self._zeroed,
            "certain": self._certain,
            "pan_deg": round(pan, 4),
            "tilt_deg": round(tilt, 4),
            "pan_steps": self._pan_steps,
            "tilt_steps": self._tilt_steps,
            "moving": self._b.wave_tx_busy(),
            "backlash_ok": {a.name: self._lash_ok(a) for a in (self.cfg.pan, self.cfg.tilt)},
            "drivers": None if self.drivers is None else {
                **self.drivers.status(), "warnings": list(self.driver_warnings)},
        }

    def _lash_ok(self, axis: AxisConfig) -> bool:
        """True when the axis last moved along approach_sign, so the gear lash
        sits on the side every absolute move arrives from."""
        return self._lash[axis.name] == axis.approach_sign

    # -- power ------------------------------------------------------------ #

    def enable(self) -> None:
        """Configure the drivers over UART and verify it, then energise them. They stay
        energised so nothing creeps. Raises (drivers stay off) if the configuration cannot
        be verified, or if real hardware has no driver bus at all. Already enabled, it checks
        first: configuring clears the reset flag, and a reset must not pass unnoticed."""
        if self.drivers is None and not isinstance(self._b, SimBackend):
            raise MountError("no driver UART: without its configuration the drivers run 1/8 "
                             "and 1/32 microstepping, so the head will not be enabled")
        if self.drivers is not None:
            self._check_drivers()  # does nothing unless already enabled
            try:
                self.drivers.configure()
            except MountError:
                self._release_unverified()
                raise
            self.driver_warnings = []
        pins = self.cfg.pins
        self._b.write(pins.enable, 0 if pins.enable_active_low else 1)
        time.sleep(self.cfg.enable_settle_ms / 1000.0)
        self._enabled = True

    def disable(self) -> None:
        """Release the drivers. The head can then be moved by hand, so the
        datum no longer means anything."""
        pins = self.cfg.pins
        self._b.write(pins.enable, 1 if pins.enable_active_low else 0)
        self._enabled = False
        self._zeroed = False
        self._lash = {"pan": None, "tilt": None}

    def _release_unverified(self) -> None:
        """The drivers no longer provably hold their configuration: motors off, datum gone,
        so nothing moves until enable() has verified them and the zero is set again."""
        if self._enabled:
            self._certain = False
            self.disable()

    # -- datum ------------------------------------------------------------ #

    def set_zero(self, pan_deg: float = 0.0, tilt_deg: float = 0.0) -> None:
        """Declare the current physical pose to be (pan_deg, tilt_deg)."""
        with self._lock:
            self._pan_steps = self.cfg.pan.deg_to_steps(pan_deg)
            self._tilt_steps = self.cfg.tilt.deg_to_steps(tilt_deg)
            self._zeroed = True
            self._certain = True
        log.info("datum set: pan=%.4f tilt=%.4f", pan_deg, tilt_deg)

    def adjust_zero(self, pan_deg: float = 0.0, tilt_deg: float = 0.0) -> None:
        """Shift the datum by a correction, e.g. from optical zeroing."""
        if not self._zeroed:
            raise NotZeroed("no datum to adjust")
        with self._lock:
            self._pan_steps += self.cfg.pan.deg_to_steps(pan_deg)
            self._tilt_steps += self.cfg.tilt.deg_to_steps(tilt_deg)

    # -- motion ----------------------------------------------------------- #

    def move_to(
        self,
        pan_deg: float,
        tilt_deg: float,
        settle: bool = True,
        speed: float = 1.0,
        stop: threading.Event | None = None,
    ) -> MoveResult:
        """Absolute move. Blocks until the head has settled.

        Both axes ride in one waveform. If either axis would arrive from the
        wrong side, the move is split: overshoot past the target, then come
        back along the configured approach direction so gear backlash is taken
        up the same way every time.

        `speed` scales the configured top speed (0 < speed <= 1). `stop` is an
        event another thread may set to ramp the move down early; pass your
        own so a stop sent before the move starts is not lost.
        """
        if not self._zeroed:
            raise NotZeroed("set_zero() before commanding an absolute angle")
        self._require_enabled()
        _check_speed(speed)

        with self._lock:
            stop = self._begin(stop)
            try:
                pan_t = self._target_steps(self.cfg.pan, pan_deg)
                tilt_t = self._target_steps(self.cfg.tilt, tilt_deg)

                pan_stage1, pan_needs = self._approach_stage(self.cfg.pan, self._pan_steps, pan_t)
                tilt_stage1, tilt_needs = self._approach_stage(
                    self.cfg.tilt, self._tilt_steps, tilt_t)
                two_stage = pan_needs or tilt_needs

                t0 = time.monotonic()
                stopped = False
                if two_stage:
                    stopped = self._run(pan_stage1 - self._pan_steps,
                                        tilt_stage1 - self._tilt_steps, speed, stop)
                if not stopped:
                    stopped = self._run(pan_t - self._pan_steps, tilt_t - self._tilt_steps,
                                        speed, stop)
                motion_s = time.monotonic() - t0

                settle_s = 0.0
                if settle and not stopped and self.cfg.settle_ms:
                    settle_s = self.cfg.settle_ms / 1000.0
                    time.sleep(settle_s)

                pan_now, tilt_now = self.position()
                return MoveResult(pan_now, tilt_now, motion_s, settle_s, two_stage, stopped)
            finally:
                self._stop = None

    def move_by(self, pan_deg: float = 0.0, tilt_deg: float = 0.0, **kw) -> MoveResult:
        pan, tilt = self.position()
        return self.move_to(pan + pan_deg, tilt + tilt_deg, **kw)

    def jog(
        self,
        axis: str,
        direction: int,
        speed: float = 1.0,
        stop: threading.Event | None = None,
    ) -> MoveResult:
        """Hold-to-move: run one axis until `stop` is set (or `stop()` is
        called), then ramp down. Blocks.

        With a datum the jog runs at most to the soft limit, ramping down in
        time to stop on it. Without one the position is unknown, so no limit
        can apply; a single jog is then capped at `unzeroed_jog_deg`, which is
        what lets the operator find the fiducial before zeroing.

        No backlash compensation: the axis is left wherever the operator let
        go, and the lash state records that it may need taking up.
        """
        if direction not in (-1, 1):
            raise ValueError("direction must be +1 or -1")
        ax = self.cfg.axis(axis)
        self._require_enabled()
        _check_speed(speed)

        with self._lock:
            stop = self._begin(stop)
            try:
                current = self._pan_steps if axis == "pan" else self._tilt_steps
                if self._zeroed:
                    target = ax.deg_to_steps(ax.max_deg if direction > 0 else ax.min_deg)
                else:
                    target = current + direction * ax.deg_to_steps(self.cfg.unzeroed_jog_deg)
                steps = target - current
                if steps == 0 or (steps > 0) != (direction > 0):
                    raise SoftLimit(
                        f"{axis} is already at its {'max' if direction > 0 else 'min'} "
                        "soft limit")

                t0 = time.monotonic()
                pan_s, tilt_s = (steps, 0) if axis == "pan" else (0, steps)
                stopped = self._run(pan_s, tilt_s, speed, stop)
                pan_now, tilt_now = self.position()
                return MoveResult(pan_now, tilt_now, time.monotonic() - t0, 0.0, False, stopped)
            finally:
                self._stop = None

    def nudge(
        self,
        pan_deg: float = 0.0,
        tilt_deg: float = 0.0,
        speed: float = 1.0,
        stop: threading.Event | None = None,
    ) -> MoveResult:
        """Relative step that always ends with the backlash taken up, so where
        a nudge lands is where a saved position replays.

        With a datum this is `move_by` (soft limits, two-stage approach).
        Without one there are no limits to check, so each nudge is capped at
        `unzeroed_jog_deg`, and the approach is restored afterwards.
        """
        _check_speed(speed)
        if self._zeroed:
            return self.move_by(pan_deg, tilt_deg, settle=False, speed=speed, stop=stop)
        self.check_nudge(pan_deg, tilt_deg)
        self._require_enabled()
        with self._lock:
            stop = self._begin(stop)
            try:
                t0 = time.monotonic()
                stopped = self._run(self.cfg.pan.deg_to_steps(pan_deg),
                                    self.cfg.tilt.deg_to_steps(tilt_deg), speed, stop)
                staged = False
                if not stopped:
                    staged, stopped = self._take_up(speed, stop)
                pan_now, tilt_now = self.position()
                return MoveResult(pan_now, tilt_now, time.monotonic() - t0, 0.0, staged, stopped)
            finally:
                self._stop = None

    def take_up_backlash(
        self, speed: float = 1.0, stop: threading.Event | None = None
    ) -> bool:
        """Leave every axis with its lash on the approach side without changing
        the commanded position: back off by `backlash_deg`, return along
        `approach_sign`. The view shifts by the actual backlash, and that is
        the point: what is then seen is what an absolute move to this angle
        reproduces. Do it before set_zero() and before recording a position.

        Needs no datum. Returns True if anything moved.
        """
        self._require_enabled()
        _check_speed(speed)
        with self._lock:
            stop = self._begin(stop)
            try:
                moved, _ = self._take_up(speed, stop)
                return moved
            finally:
                self._stop = None

    def jog_steps(self, pan_steps: int = 0, tilt_steps: int = 0) -> None:
        """Raw relative move, no limit check, no backlash compensation, no
        datum required. For bench work only - finding mechanical interference,
        checking direction sense, setting Vref under load."""
        self._require_enabled()
        with self._lock:
            stop = self._begin(None)
            try:
                self._run(pan_steps, tilt_steps, 1.0, stop)
            finally:
                self._stop = None

    def stop(self) -> None:
        """Ramp the running motion down from another thread. The move returns
        early with `stopped=True`; the position stays exact. A no-op when
        idle - pass your own `stop` event to a motion call to avoid racing its
        start."""
        ev = self._stop
        if ev is not None:
            ev.set()

    def abort(self) -> None:
        """Stop transmitting from another thread. The in-progress move raises
        MoveAborted and position becomes an estimate."""
        self._abort.set()

    def _begin(self, stop: threading.Event | None) -> threading.Event:
        self._check_drivers()
        self._abort.clear()
        self._stop = stop or threading.Event()
        return self._stop

    def check_drivers(self) -> list[str]:
        """The check made before every motion, for a head that is held without moving. Same
        consequences; returns the warnings."""
        with self._lock:
            self._check_drivers()
            return list(self.driver_warnings)

    def _check_drivers(self) -> None:
        """Before every motion: a driver reset (motor rail cycled) means the motors were
        unpowered, so the position is marked uncertain and the move refused; faults refuse
        too. A reset that cannot be reconfigured releases the motors. Warnings (pre-warning
        temperature, open load) are kept for status()."""
        if self.drivers is None or not self._enabled:
            return
        from .tmc2209 import DriverReset

        try:
            self.driver_warnings = self.drivers.check()
        except DriverReset:
            self._certain = False
            raise
        except MountError:
            if not self.drivers.configured:
                self._release_unverified()
            raise

    def _require_enabled(self) -> None:
        if not self._enabled:
            raise MountError("drivers are not enabled")

    def _take_up(self, speed: float, stop: threading.Event) -> tuple[bool, bool]:
        """(moved, stopped). Caller holds the lock."""
        back: dict[str, int] = {}
        for ax, current in ((self.cfg.pan, self._pan_steps), (self.cfg.tilt, self._tilt_steps)):
            margin = ax.deg_to_steps(ax.backlash_deg)
            off = 0
            if margin > 0 and not self._lash_ok(ax):
                target = current - ax.approach_sign * margin
                if self._zeroed:  # stay inside the soft limits when they mean something
                    lo, hi = ax.deg_to_steps(ax.min_deg), ax.deg_to_steps(ax.max_deg)
                    target = max(lo, min(hi, target))
                off = target - current
                if off == 0:
                    log.warning("%s: no room inside the soft limit to take up backlash",
                                ax.name)
            back[ax.name] = off
        if not any(back.values()):
            return False, False
        if self._run(back["pan"], back["tilt"], speed, stop):
            return True, True
        return True, self._run(-back["pan"], -back["tilt"], speed, stop)

    def wait_idle(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        while self._b.wave_tx_busy():
            if deadline and time.monotonic() > deadline:
                return False
            time.sleep(POLL_S)
        return True

    def check_target(self, pan_deg: float, tilt_deg: float) -> None:
        """Raise SoftLimit if an absolute target is outside the soft limits. Moves nothing;
        lets a caller refuse a request before starting a worker thread for it."""
        self._target_steps(self.cfg.pan, pan_deg)
        self._target_steps(self.cfg.tilt, tilt_deg)

    def check_nudge(self, pan_deg: float, tilt_deg: float) -> None:
        """Raise SoftLimit if `nudge(pan_deg, tilt_deg)` would be refused."""
        if self._zeroed:
            pan, tilt = self.position()
            self.check_target(pan + pan_deg, tilt + tilt_deg)
            return
        cap = self.cfg.unzeroed_jog_deg
        if abs(pan_deg) > cap or abs(tilt_deg) > cap:
            raise SoftLimit(f"no datum yet: a single nudge is capped at {cap:g} deg")

    # -- planning --------------------------------------------------------- #

    def _target_steps(self, axis: AxisConfig, deg: float) -> int:
        if not (axis.min_deg <= deg <= axis.max_deg):
            raise SoftLimit(
                f"{axis.name} target {deg:.3f} deg outside "
                f"[{axis.min_deg}, {axis.max_deg}] - there are no limit switches"
            )
        return axis.deg_to_steps(deg)

    def _approach_stage(
        self, axis: AxisConfig, current: int, target: int
    ) -> tuple[int, bool]:
        """Where stage 1 must land so the final approach runs in approach_sign.

        Returns (stage1_target_steps, needed). `needed` is False when the
        direct move already arrives from the correct side.
        """
        margin = axis.deg_to_steps(axis.backlash_deg)
        if margin <= 0:
            return target, False

        delta = target - current
        if delta == 0:
            if self._lash_ok(axis):
                return target, False  # already there, lash on the approach side
            # Already there, but a jog, nudge or abort left the lash the other
            # way: back off and come in again.
        elif (delta > 0) == (axis.approach_sign > 0):
            return target, False  # already arriving the right way

        overshoot = target - axis.approach_sign * margin
        lo = axis.deg_to_steps(axis.min_deg)
        hi = axis.deg_to_steps(axis.max_deg)
        clamped = max(lo, min(hi, overshoot))
        if clamped != overshoot:
            log.warning(
                "%s: backlash overshoot clipped by soft limit (%d -> %d steps)",
                axis.name,
                overshoot,
                clamped,
            )
        if clamped == target:
            return target, False
        return clamped, True

    def _plans(self, pan_steps: int, tilt_steps: int,
               speed: float = 1.0) -> tuple[AxisPlan, AxisPlan]:
        def plan(axis: AxisConfig, steps: int) -> AxisPlan:
            spd = axis.steps_per_deg
            top = axis.max_speed_deg_s * speed
            return AxisPlan(
                steps=steps,
                max_speed_sps=top * spd,
                accel_sps2=axis.accel_deg_s2 * spd,
                start_speed_sps=min(axis.start_speed_deg_s, top) * spd,
            )

        return plan(self.cfg.pan, pan_steps), plan(self.cfg.tilt, tilt_steps)

    # -- transmission ----------------------------------------------------- #

    def _run(self, pan_steps: int, tilt_steps: int, speed: float = 1.0,
             stop: threading.Event | None = None) -> bool:
        """Emit one two-axis move. Returns True if `stop` cut it short."""
        if pan_steps == 0 and tilt_steps == 0:
            return False

        self._set_dir(self.cfg.pan, self.cfg.pins.pan_dir, pan_steps)
        self._set_dir(self.cfg.tilt, self.cfg.pins.tilt_dir, tilt_steps)
        if self.cfg.dir_setup_us:
            time.sleep(self.cfg.dir_setup_us / 1e6)

        pan_plan, tilt_plan = self._plans(pan_steps, tilt_steps, speed)
        events = plan_timeline(pan_plan, tilt_plan)
        if stop is not None:
            events = stoppable(events, pan_plan, tilt_plan, stop.is_set)
        chunks = build_chunks(
            events,
            self.cfg.pins.pan_step,
            self.cfg.pins.tilt_step,
            self.cfg.pulse_width_us,
            self._max_pulses,
            CHUNK_US,
        )

        pan_sign = 1 if pan_steps > 0 else -1
        tilt_sign = 1 if tilt_steps > 0 else -1
        done_pan, done_tilt, partial = self._stream(chunks)

        with self._lock:
            self._pan_steps += pan_sign * done_pan
            self._tilt_steps += tilt_sign * done_tilt
            if done_pan:
                self._lash["pan"] = pan_sign
            if done_tilt:
                self._lash["tilt"] = tilt_sign

        if partial:
            self._certain = False
            raise MoveAborted(
                f"move aborted after ~{done_pan} pan / ~{done_tilt} tilt steps; "
                "position is an estimate, re-zero before capturing"
            )

        complete = done_pan == abs(pan_steps) and done_tilt == abs(tilt_steps)
        stopped = stop is not None and stop.is_set() and not complete
        short = done_pan > abs(pan_steps) or done_tilt > abs(tilt_steps)
        if short or (not complete and not stopped):
            self._certain = False
            raise MountError(
                f"step accounting mismatch: emitted {done_pan}/{done_tilt}, "
                f"planned {abs(pan_steps)}/{abs(tilt_steps)}"
            )
        return stopped

    def _set_dir(self, axis: AxisConfig, gpio: int, steps: int) -> None:
        if steps == 0:
            return
        forward = steps > 0
        level = 1 if forward != axis.invert else 0
        self._b.write(gpio, level)

    def _stream(self, chunks: Iterable[WaveChunk]) -> tuple[int, int, bool]:
        """Feed chunks to the DMA engine back to back.

        Only MAX_WAVES_IN_FLIGHT waves exist at a time, so a 180 deg move costs
        the same memory as a 1 deg one. Sync mode makes each wave start the
        instant the previous finishes, which is what keeps the timeline seamless
        across a chunk boundary.
        """
        inflight: deque[_InFlight] = deque()
        pan_done = tilt_done = 0
        aborted = False

        def retire_finished() -> None:
            nonlocal pan_done, tilt_done
            now = time.monotonic()
            at = self._b.wave_tx_at()
            if inflight and at == inflight[0].wave_id and inflight[0].started_at is None:
                inflight[0].started_at = now
            while inflight:
                head = inflight[0]
                later = {w.wave_id for w in list(inflight)[1:]}
                # Either a later wave is on air (so this one is certainly done),
                # or nothing is transmitting. The grace period covers the gap
                # between handing a wave to the DMA engine and it going busy.
                finished = at in later or (
                    not self._b.wave_tx_busy()
                    and (now - head.queued_at) >= self._b.tx_start_grace_s
                )
                if not finished:
                    break
                inflight.popleft()
                pan_done += head.pan_steps
                tilt_done += head.tilt_steps
                self._b.wave_delete(head.wave_id)

        try:
            for chunk in chunks:
                while len(inflight) >= MAX_WAVES_IN_FLIGHT:
                    if self._abort.is_set():
                        raise _Abort
                    retire_finished()
                    if len(inflight) >= MAX_WAVES_IN_FLIGHT:
                        time.sleep(POLL_S)
                if self._abort.is_set():
                    raise _Abort

                self._b.wave_add_generic(chunk.pulses)
                wave_id = self._b.wave_create()

                if self._b.wave_tx_busy():
                    self._b.wave_send_sync(wave_id)  # starts the instant the
                else:                                # current wave ends - no seam
                    self._b.wave_send_once(wave_id)

                inflight.append(
                    _InFlight(
                        wave_id=wave_id,
                        pan_steps=chunk.pan_steps,
                        tilt_steps=chunk.tilt_steps,
                        duration_s=chunk.duration_us / 1e6,
                        queued_at=time.monotonic(),
                    )
                )

            while inflight:
                if self._abort.is_set():
                    raise _Abort
                retire_finished()
                if inflight:
                    time.sleep(POLL_S)

        except _Abort:
            aborted = True
            self._b.wave_tx_stop()
            now = time.monotonic()
            for idx, wave in enumerate(inflight):
                if idx == 0 and wave.duration_s > 0:
                    began = wave.started_at or wave.queued_at
                    frac = min(1.0, max(0.0, (now - began) / wave.duration_s))
                    pan_done += int(round(wave.pan_steps * frac))
                    tilt_done += int(round(wave.tilt_steps * frac))
                self._b.wave_delete(wave.wave_id)
            inflight.clear()

        return pan_done, tilt_done, aborted

    # -- shutdown --------------------------------------------------------- #

    def _safe_shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._b.wave_tx_stop()
        except Exception:
            pass
        try:
            pins = self.cfg.pins
            self._b.write(pins.enable, 1 if pins.enable_active_low else 0)
        except Exception:
            pass
        self._enabled = False
        self._zeroed = False
        self._lash = {"pan": None, "tilt": None}

    def close(self) -> None:
        self._safe_shutdown()
        try:
            self._b.close()
        except Exception:
            pass
        if self.drivers is not None:
            try:
                self.drivers.close()
            except Exception:
                pass

    def __enter__(self) -> "PanTiltMount":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class _Abort(Exception):
    pass


def _check_speed(speed: float) -> None:
    if not 0.0 < speed <= 1.0:
        raise ValueError(f"speed is a fraction of the configured maximum, got {speed}")


def install_signal_handlers(mount: PanTiltMount) -> None:
    """Drop ENABLE and stop the DMA engine on SIGINT/SIGTERM.

    Not done in the constructor on purpose - installing handlers is the
    application's call, not a library's. The CLI and the pipeline service
    should call this; unit tests should not.
    """

    def handler(signum, frame):
        mount.abort()
        mount.close()
        raise SystemExit(128 + signum)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handler)
