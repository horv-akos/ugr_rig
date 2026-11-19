"""Trapezoidal motion planning and waveform assembly.

Nothing in here touches hardware. It turns "move pan N steps and tilt M steps"
into a list of pigpio pulses, chunked so no single wave exceeds the DMA limit.

Model
-----
Each axis gets its own trapezoidal profile (linear ramp in step frequency,
cruise, symmetric ramp down). The two profiles will have different natural
durations, so the shorter one is stretched in time until both axes start and
finish together. Stretching only ever lowers speed and acceleration, so the
slower axis is never pushed past its limits.

The resulting step instants from both axes are merged into one timeline and
emitted as a single pulse stream, which is what lets both STEP pins run on
independent schedules inside one transmission.

Controlled stop
---------------
Waves already handed to the DMA engine cannot be edited, and stopping one
mid-flight leaves the step count unknown (that is what abort does). So a stop
has to be planned: the timeline is consumed lazily, and once a stop is
requested each axis ramps down from its current rate instead of finishing the
plan. Waves are also capped in duration, so the stop starts within a few wave
lengths. The step count stays exact.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import floor, sqrt
from typing import Callable, Iterable, Iterator

from .backend import Pulse

PAN = 0
TILT = 1


@dataclass(frozen=True)
class AxisPlan:
    """One axis' contribution to a move."""

    steps: int  # signed; sign selects DIR level
    max_speed_sps: float
    accel_sps2: float
    start_speed_sps: float


@dataclass(frozen=True)
class WaveChunk:
    """A slice of the move small enough to fit in one pigpio wave."""

    pulses: list[Pulse]
    pan_steps: int  # unsigned count in this chunk
    tilt_steps: int
    duration_us: int


# --------------------------------------------------------------------------- #
# Profile
# --------------------------------------------------------------------------- #


def step_times(n: int, f_max: float, f_start: float, accel: float) -> list[float]:
    """Instant of each of `n` steps, seconds from the start of motion.

    f_max / f_start in steps/s, accel in steps/s^2. A non-zero f_start keeps the
    first interval finite; steppers have no trouble starting at a few steps/s.
    Degenerates to a triangular profile when the move is too short to reach
    f_max, and to constant speed when accel <= 0.
    """
    if n <= 0:
        return []
    f_start = max(1e-6, min(f_start, f_max))
    if accel <= 0:
        return [(i + 1) / f_max for i in range(n)]

    # Steps consumed by one full ramp from f_start to f_max.
    ramp_steps = (f_max * f_max - f_start * f_start) / (2.0 * accel)
    if 2.0 * ramp_steps >= n:
        ramp_steps = n / 2.0
        f_peak = sqrt(f_start * f_start + accel * n)
    else:
        f_peak = f_max

    t_ramp = (f_peak - f_start) / accel
    cruise_steps = n - 2.0 * ramp_steps
    t_cruise = cruise_steps / f_peak
    total = 2.0 * t_ramp + t_cruise

    f0sq = f_start * f_start
    out: list[float] = []
    for i in range(1, n + 1):
        s = float(i)
        if s <= ramp_steps:
            t = (sqrt(f0sq + 2.0 * accel * s) - f_start) / accel
        elif s <= ramp_steps + cruise_steps:
            t = t_ramp + (s - ramp_steps) / f_peak
        else:
            remaining = n - s  # mirror the ramp-up about the midpoint
            t = total - (sqrt(f0sq + 2.0 * accel * remaining) - f_start) / accel
        out.append(t)
    return out


def plan_timeline(pan: AxisPlan, tilt: AxisPlan) -> list[tuple[float, int]]:
    """Merged (time, axis) step events for a synchronised two-axis move."""
    t_pan = step_times(abs(pan.steps), pan.max_speed_sps, pan.start_speed_sps, pan.accel_sps2)
    t_tilt = step_times(abs(tilt.steps), tilt.max_speed_sps, tilt.start_speed_sps, tilt.accel_sps2)

    dur_pan = t_pan[-1] if t_pan else 0.0
    dur_tilt = t_tilt[-1] if t_tilt else 0.0
    duration = max(dur_pan, dur_tilt)

    # Stretch the quicker axis onto the common duration.
    if t_pan and dur_pan > 0 and duration > dur_pan:
        k = duration / dur_pan
        t_pan = [t * k for t in t_pan]
    if t_tilt and dur_tilt > 0 and duration > dur_tilt:
        k = duration / dur_tilt
        t_tilt = [t * k for t in t_tilt]

    events = [(t, PAN) for t in t_pan]
    events.extend((t, TILT) for t in t_tilt)
    events.sort()
    return events


def move_duration(pan: AxisPlan, tilt: AxisPlan) -> float:
    """Seconds of motion, excluding settle. Useful for progress reporting."""
    events = plan_timeline(pan, tilt)
    return events[-1][0] if events else 0.0


# --------------------------------------------------------------------------- #
# Controlled stop
# --------------------------------------------------------------------------- #


def ramp_down_times(f: float, f_end: float, accel: float) -> list[float]:
    """Step instants, seconds after the last step at rate `f`, of a linear ramp
    down to `f_end`. Empty when there is nothing to ramp (already slow, or no
    acceleration limit configured)."""
    if accel <= 0 or f <= f_end:
        return []
    n = floor((f * f - f_end * f_end) / (2.0 * accel))
    return [(f - sqrt(max(0.0, f * f - 2.0 * accel * k))) / accel for k in range(1, n + 1)]


def stoppable(
    events: list[tuple[float, int]],
    pan: AxisPlan,
    tilt: AxisPlan,
    should_stop: Callable[[], bool],
) -> Iterator[tuple[float, int]]:
    """Yield the planned events; once `should_stop()` is true, ramp each axis
    down from its current rate instead.

    The current rate is taken from the last two steps actually yielded on that
    axis. An axis whose remaining plan is shorter than the ramp-down simply
    finishes the plan, which already ends in a ramp. An axis that has not
    stepped twice yet stops where it is.
    """
    plans = (pan, tilt)
    remaining = [abs(pan.steps), abs(tilt.steps)]
    hist: tuple[list[float], list[float]] = ([], [])
    last_t = 0.0
    i = 0
    stopped = False
    while i < len(events):
        if should_stop():
            stopped = True
            break
        t, ax = events[i]
        i += 1
        remaining[ax] -= 1
        h = hist[ax]
        h.append(t)
        del h[:-2]
        last_t = t
        yield t, ax
    if not stopped:
        return

    tail: list[tuple[float, int]] = []
    for ax, plan in enumerate(plans):
        h = hist[ax]
        if remaining[ax] <= 0 or len(h) < 2:
            continue
        f = 1.0 / max(h[1] - h[0], 1e-9)
        f_end = min(plan.start_speed_sps, plan.max_speed_sps)
        ramp = ramp_down_times(f, f_end, plan.accel_sps2)
        if len(ramp) >= remaining[ax]:
            tail.extend(e for e in events[i:] if e[1] == ax)
        else:
            tail.extend((h[1] + dt, ax) for dt in ramp)
    tail.sort()
    for t, ax in tail:
        last_t = max(t, last_t)  # never step back behind what is already queued
        yield last_t, ax


# --------------------------------------------------------------------------- #
# Waveform assembly
# --------------------------------------------------------------------------- #


def build_chunks(
    events: Iterable[tuple[float, int]],
    pan_step_gpio: int,
    tilt_step_gpio: int,
    pulse_width_us: int,
    max_pulses: int,
    max_chunk_us: int | None = None,
) -> Iterator[WaveChunk]:
    """Turn a merged event timeline into pigpio waves of bounded length.

    Each step is a rising edge followed `pulse_width_us` later by a falling one.
    Edges landing in the same microsecond are coalesced into one pulse, so two
    axes stepping together cost one entry rather than two.

    Chunks are cut on step boundaries and carry the exact inter-chunk delay, so
    transmitting them back to back in sync mode reproduces the timeline without
    a seam. `max_chunk_us` also caps each chunk's duration, which bounds how
    far ahead of a stop request the queued waves reach.

    Events are consumed lazily, one past the end of each chunk, so a stoppable
    timeline sees a stop request at most one chunk late.
    """
    masks = {PAN: 1 << pan_step_gpio, TILT: 1 << tilt_step_gpio}
    # Two pulse entries per step worst case, plus headroom for the tail delay.
    per_chunk = max(1, (max_pulses - 4) // 2)

    cursor_us = 0  # absolute position of the waveform cursor after the last chunk
    for window in _windows(events, per_chunk, max_chunk_us):
        edges: dict[int, list[int]] = {}  # us -> [on_mask, off_mask]
        pan_steps = tilt_steps = 0
        for t, axis in window:
            on_us = int(round(t * 1e6))
            off_us = on_us + pulse_width_us
            edges.setdefault(on_us, [0, 0])[0] |= masks[axis]
            edges.setdefault(off_us, [0, 0])[1] |= masks[axis]
            if axis == PAN:
                pan_steps += 1
            else:
                tilt_steps += 1

        keys = sorted(edges)
        for us in keys:
            on, off = edges[us]
            if on & off:
                raise ValueError(
                    "step rate too high for the configured pulse width: a rising "
                    "and falling edge collided on the same pin"
                )

        pulses: list[Pulse] = []

        # Lead-in: idle gap between where the cursor sits and this chunk's first
        # edge. For chunk 0 that is the delay before the very first step.
        lead = keys[0] - cursor_us
        if lead > 0:
            pulses.append(Pulse(0, 0, lead))

        for idx, us in enumerate(keys):
            on, off = edges[us]
            delay = keys[idx + 1] - us if idx + 1 < len(keys) else 1
            pulses.append(Pulse(on, off, max(1, delay)))

        cursor_us = keys[-1] + 1  # the 1 us spent closing the last falling edge
        yield WaveChunk(pulses, pan_steps, tilt_steps, sum(p.delay_us for p in pulses))


def _windows(
    events: Iterable[tuple[float, int]], per_chunk: int, max_chunk_us: int | None
) -> Iterator[list[tuple[float, int]]]:
    """Group events into chunks of at most `per_chunk` steps, and of at most
    `max_chunk_us` measured from the end of the previous chunk."""
    window: list[tuple[float, int]] = []
    start_us = 0.0
    for ev in events:
        t_us = ev[0] * 1e6
        if window and (
            len(window) >= per_chunk
            or (max_chunk_us is not None and t_us - start_us >= max_chunk_us)
        ):
            start_us = window[-1][0] * 1e6
            yield window
            window = []
        window.append(ev)
    if window:
        yield window
