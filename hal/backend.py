"""Pulse backend abstraction.

`hal.mount` never imports pigpio directly. It talks to a PulseBackend, which is
either the real pigpio client (on the Pi) or a simulator (on a desk). The
simulator accounts every edge, so the step maths, backlash handling and wave
chunking can be exercised without hardware.

The surface deliberately mirrors the pigpio calls we actually use. It is not a
general GPIO abstraction.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol, Sequence, runtime_checkable

# pigpio wave transmission modes, restated so the simulator does not need pigpio.
WAVE_MODE_ONE_SHOT = 0
WAVE_MODE_ONE_SHOT_SYNC = 2

# Returned by wave_tx_at() when nothing is being transmitted.
NO_TX = 9999


@dataclass(frozen=True)
class Pulse:
    """One pigpio waveform entry: raise these pins, lower those, then wait."""

    on_mask: int
    off_mask: int
    delay_us: int


@runtime_checkable
class PulseBackend(Protocol):
    def set_output(self, gpio: int, initial: int) -> None: ...
    def write(self, gpio: int, level: int) -> None: ...
    def wave_clear(self) -> None: ...
    def wave_add_generic(self, pulses: Sequence[Pulse]) -> int: ...
    def wave_create(self) -> int: ...
    def wave_delete(self, wave_id: int) -> None: ...
    def wave_send_once(self, wave_id: int) -> int: ...
    def wave_send_sync(self, wave_id: int) -> int: ...
    def wave_tx_at(self) -> int: ...
    def wave_tx_busy(self) -> bool: ...
    def wave_tx_stop(self) -> None: ...
    def wave_get_max_pulses(self) -> int: ...
    # How long after queueing a wave the engine may still report idle. The mount
    # will not count a wave finished before this has passed.
    tx_start_grace_s: float
    def close(self) -> None: ...


# --------------------------------------------------------------------------- #
# pigpio
# --------------------------------------------------------------------------- #


class PigpioBackend:
    """Thin adapter over the pigpio socket client.

    Requires `pigpiod` to be running. Start it with `-t 1` so it uses the PCM
    peripheral for timing rather than PWM (see docs/PIVOT_DETAIL.md §4.2).
    """

    tx_start_grace_s = 0.02  # the DMA engine may not report busy the instant a wave is queued

    def __init__(self, host: str = "localhost", port: int = 8888) -> None:
        import pigpio  # imported here so the module loads on a desk

        self._pigpio = pigpio
        self._pi = pigpio.pi(host, port)
        if not self._pi.connected:
            raise RuntimeError(
                f"cannot reach pigpiod at {host}:{port} - is the daemon running?"
            )
        self._pi.wave_clear()

    def set_output(self, gpio: int, initial: int) -> None:
        self._pi.write(gpio, initial)  # set level first, then direction
        self._pi.set_mode(gpio, self._pigpio.OUTPUT)
        self._pi.write(gpio, initial)

    def write(self, gpio: int, level: int) -> None:
        self._pi.write(gpio, level)

    def wave_clear(self) -> None:
        self._pi.wave_clear()

    def wave_add_generic(self, pulses: Sequence[Pulse]) -> int:
        native = [
            self._pigpio.pulse(p.on_mask, p.off_mask, p.delay_us) for p in pulses
        ]
        return self._pi.wave_add_generic(native)

    def wave_create(self) -> int:
        wid = self._pi.wave_create()
        if wid < 0:
            raise RuntimeError(f"wave_create failed: {wid}")
        return wid

    def wave_delete(self, wave_id: int) -> None:
        self._pi.wave_delete(wave_id)

    def wave_send_once(self, wave_id: int) -> int:
        return self._pi.wave_send_using_mode(wave_id, WAVE_MODE_ONE_SHOT)

    def wave_send_sync(self, wave_id: int) -> int:
        return self._pi.wave_send_using_mode(wave_id, WAVE_MODE_ONE_SHOT_SYNC)

    def wave_tx_at(self) -> int:
        return self._pi.wave_tx_at()

    def wave_tx_busy(self) -> bool:
        return bool(self._pi.wave_tx_busy())

    def wave_tx_stop(self) -> None:
        self._pi.wave_tx_stop()

    def wave_get_max_pulses(self) -> int:
        return self._pi.wave_get_max_pulses()

    def close(self) -> None:
        try:
            self._pi.wave_tx_stop()
            self._pi.wave_clear()
        finally:
            self._pi.stop()


# --------------------------------------------------------------------------- #
# Simulator
# --------------------------------------------------------------------------- #


@dataclass
class _SimWave:
    pulses: list[Pulse]
    duration_us: int


@dataclass
class SimBackend:
    """In-process stand-in for pigpio.

    Counts rising edges per GPIO and records the DIR level in force at each one,
    which is everything needed to check that a planned move would have produced
    the intended number of steps in the intended direction.

    `time_scale` multiplies simulated wave duration into wall-clock sleep:
    0.0 runs instantly (tests, dry runs), 1.0 runs in real time (CLI demos).
    """

    time_scale: float = 0.0
    max_pulses: int = 12000
    tx_start_grace_s: float = 0.0  # busy is exact here, so no grace is needed

    levels: dict[int, int] = field(default_factory=dict)
    rising_edges: dict[int, int] = field(default_factory=dict)
    edge_log: list[tuple[int, int, dict[int, int]]] = field(default_factory=list)
    total_us: int = 0

    _staged: list[Pulse] = field(default_factory=list, init=False)
    _waves: dict[int, _SimWave] = field(default_factory=dict, init=False)
    _next_id: int = field(default=0, init=False)
    _tx_id: int = field(default=NO_TX, init=False)
    _tx_ends_at: float = field(default=0.0, init=False)
    _pending: list[int] = field(default_factory=list, init=False)

    # -- gpio ------------------------------------------------------------- #

    def set_output(self, gpio: int, initial: int) -> None:
        self.levels[gpio] = initial
        self.rising_edges.setdefault(gpio, 0)

    def write(self, gpio: int, level: int) -> None:
        self.levels[gpio] = level

    # -- waves ------------------------------------------------------------ #

    def wave_clear(self) -> None:
        self._staged.clear()
        self._waves.clear()
        self._next_id = 0
        self._tx_id = NO_TX
        self._pending.clear()

    def wave_add_generic(self, pulses: Sequence[Pulse]) -> int:
        self._staged.extend(pulses)
        if len(self._staged) > self.max_pulses:
            raise RuntimeError(
                f"wave too long: {len(self._staged)} > {self.max_pulses} pulses"
            )
        return len(self._staged)

    def wave_create(self) -> int:
        wid = self._next_id
        while wid in self._waves:
            wid += 1
        self._next_id = wid + 1
        duration = sum(p.delay_us for p in self._staged)
        self._waves[wid] = _SimWave(list(self._staged), duration)
        self._staged.clear()
        return wid

    def wave_delete(self, wave_id: int) -> None:
        self._waves.pop(wave_id, None)

    def wave_send_once(self, wave_id: int) -> int:
        self._advance()
        self._start(wave_id)
        return len(self._waves[wave_id].pulses)

    def wave_send_sync(self, wave_id: int) -> int:
        self._advance()
        if self._tx_id == NO_TX:
            self._start(wave_id)
        else:
            self._pending.append(wave_id)
        return len(self._waves[wave_id].pulses)

    def wave_tx_at(self) -> int:
        self._advance()
        return self._tx_id

    def wave_tx_busy(self) -> bool:
        self._advance()
        return self._tx_id != NO_TX

    def wave_tx_stop(self) -> None:
        self._tx_id = NO_TX
        self._pending.clear()

    def wave_get_max_pulses(self) -> int:
        return self.max_pulses

    def close(self) -> None:
        self.wave_clear()

    # -- internals -------------------------------------------------------- #

    def _start(self, wave_id: int) -> None:
        wave = self._waves[wave_id]
        self._apply(wave)
        self._tx_id = wave_id
        self._tx_ends_at = time.monotonic() + wave.duration_us * 1e-6 * self.time_scale

    def _advance(self) -> None:
        while self._tx_id != NO_TX and time.monotonic() >= self._tx_ends_at:
            self._tx_id = NO_TX
            if self._pending:
                self._start(self._pending.pop(0))

    def _apply(self, wave: _SimWave) -> None:
        """Replay a wave's edges into the counters at creation-order speed."""
        for pulse in wave.pulses:
            for gpio in _bits(pulse.on_mask):
                if self.levels.get(gpio, 0) == 0:
                    self.rising_edges[gpio] = self.rising_edges.get(gpio, 0) + 1
                    self.edge_log.append((self.total_us, gpio, dict(self.levels)))
                self.levels[gpio] = 1
            for gpio in _bits(pulse.off_mask):
                self.levels[gpio] = 0
            self.total_us += pulse.delay_us


def _bits(mask: int) -> list[int]:
    out = []
    bit = 0
    while mask:
        if mask & 1:
            out.append(bit)
        mask >>= 1
        bit += 1
    return out


def create_backend(kind: str, **kwargs) -> PulseBackend:
    if kind == "pigpio":
        return PigpioBackend(**kwargs)
    if kind == "sim":
        return SimBackend(**kwargs)
    raise ValueError(f"unknown backend: {kind!r}")
