"""Pan-tilt head driver.

Hardware-only. Optical zeroing needs the camera as well and belongs in
`pipeline/`; this module must stay runnable (against the sim backend) with
nothing attached.

Position is held as signed steps from the zero datum. Degrees are a derived
view, so no rounding error accumulates over a session.
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

from .backend import PulseBackend, create_backend
from .config import AxisConfig, MountConfig, load_config
from .motion import AxisPlan, WaveChunk, build_chunks, plan_timeline

log = logging.getLogger(__name__)

MAX_WAVES_IN_FLIGHT = 3
POLL_S = 0.005
TX_START_GRACE_S = 0.02  # DMA engine may not report busy the instant a wave is queued


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


class PanTiltMount:
    def __init__(self, config: MountConfig, backend: PulseBackend) -> None:
        self.cfg = config
        self._b = backend
        self._pan_steps = 0
        self._tilt_steps = 0
        self._zeroed = False
        self._certain = True
        self._enabled = False
        self._abort = threading.Event()
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
        return cls(cfg, create_backend(backend, **backend_kwargs))

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
        }

    # -- power ------------------------------------------------------------ #

    def enable(self) -> None:
        """Energise both drivers. They stay energised so nothing creeps."""
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
    ) -> MoveResult:
        """Absolute move. Blocks until the head has settled.

        Both axes ride in one waveform. If either axis would arrive from the
        wrong side, the move is split: overshoot past the target, then come
        back along the configured approach direction so gear backlash is taken
        up the same way every time.
        """
        if not self._zeroed:
            raise NotZeroed("set_zero() before commanding an absolute angle")
        if not self._enabled:
            raise MountError("drivers are not enabled")

        with self._lock:
            self._abort.clear()
            pan_t = self._target_steps(self.cfg.pan, pan_deg)
            tilt_t = self._target_steps(self.cfg.tilt, tilt_deg)

            pan_stage1, pan_needs = self._approach_stage(self.cfg.pan, self._pan_steps, pan_t)
            tilt_stage1, tilt_needs = self._approach_stage(self.cfg.tilt, self._tilt_steps, tilt_t)
            two_stage = pan_needs or tilt_needs

            t0 = time.monotonic()
            if two_stage:
                self._run(pan_stage1 - self._pan_steps, tilt_stage1 - self._tilt_steps)
            self._run(pan_t - self._pan_steps, tilt_t - self._tilt_steps)
            motion_s = time.monotonic() - t0

            settle_s = 0.0
            if settle and self.cfg.settle_ms:
                settle_s = self.cfg.settle_ms / 1000.0
                time.sleep(settle_s)

            pan_now, tilt_now = self.position()
            return MoveResult(pan_now, tilt_now, motion_s, settle_s, two_stage)

    def move_by(self, pan_deg: float = 0.0, tilt_deg: float = 0.0, **kw) -> MoveResult:
        pan, tilt = self.position()
        return self.move_to(pan + pan_deg, tilt + tilt_deg, **kw)

    def jog_steps(self, pan_steps: int = 0, tilt_steps: int = 0) -> None:
        """Raw relative move, no limit check, no backlash compensation, no
        datum required. For bench work only - finding mechanical interference,
        checking direction sense, setting Vref under load."""
        if not self._enabled:
            raise MountError("drivers are not enabled")
        with self._lock:
            self._abort.clear()
            self._run(pan_steps, tilt_steps)

    def abort(self) -> None:
        """Stop transmitting from another thread. The in-progress move raises
        MoveAborted and position becomes an estimate."""
        self._abort.set()

    def wait_idle(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        while self._b.wave_tx_busy():
            if deadline and time.monotonic() > deadline:
                return False
            time.sleep(POLL_S)
        return True

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
            # Already there, and every completed move ends on a correct
            # approach, so the backlash is already taken up the right way.
            # (jog_steps and an aborted move break that, which is why both
            # invalidate the datum.)
            return target, False
        if (delta > 0) == (axis.approach_sign > 0):
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

    def _plans(self, pan_steps: int, tilt_steps: int) -> tuple[AxisPlan, AxisPlan]:
        def plan(axis: AxisConfig, steps: int) -> AxisPlan:
            spd = axis.steps_per_deg
            return AxisPlan(
                steps=steps,
                max_speed_sps=axis.max_speed_deg_s * spd,
                accel_sps2=axis.accel_deg_s2 * spd,
                start_speed_sps=axis.start_speed_deg_s * spd,
            )

        return plan(self.cfg.pan, pan_steps), plan(self.cfg.tilt, tilt_steps)

    # -- transmission ----------------------------------------------------- #

    def _run(self, pan_steps: int, tilt_steps: int) -> None:
        if pan_steps == 0 and tilt_steps == 0:
            return

        self._set_dir(self.cfg.pan, self.cfg.pins.pan_dir, pan_steps)
        self._set_dir(self.cfg.tilt, self.cfg.pins.tilt_dir, tilt_steps)
        if self.cfg.dir_setup_us:
            time.sleep(self.cfg.dir_setup_us / 1e6)

        pan_plan, tilt_plan = self._plans(pan_steps, tilt_steps)
        events = plan_timeline(pan_plan, tilt_plan)
        chunks = build_chunks(
            events,
            self.cfg.pins.pan_step,
            self.cfg.pins.tilt_step,
            self.cfg.pulse_width_us,
            self._max_pulses,
        )

        pan_sign = 1 if pan_steps > 0 else -1
        tilt_sign = 1 if tilt_steps > 0 else -1
        done_pan, done_tilt, partial = self._stream(chunks)

        with self._lock:
            self._pan_steps += pan_sign * done_pan
            self._tilt_steps += tilt_sign * done_tilt

        if partial:
            self._certain = False
            raise MoveAborted(
                f"move aborted after ~{done_pan} pan / ~{done_tilt} tilt steps; "
                "position is an estimate, re-zero before capturing"
            )

        if done_pan != abs(pan_steps) or done_tilt != abs(tilt_steps):
            self._certain = False
            raise MountError(
                f"step accounting mismatch: emitted {done_pan}/{done_tilt}, "
                f"planned {abs(pan_steps)}/{abs(tilt_steps)}"
            )

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
                    and (now - head.queued_at) >= TX_START_GRACE_S
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

    def close(self) -> None:
        self._safe_shutdown()
        try:
            self._b.close()
        except Exception:
            pass

    def __enter__(self) -> "PanTiltMount":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class _Abort(Exception):
    pass


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
