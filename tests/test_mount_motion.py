"""Controlled stop, hold-to-move jog, speed and backlash tracking, against SimBackend."""

from __future__ import annotations

import threading
import time
from dataclasses import replace

import pytest

from hal.backend import SimBackend
from hal.config import load_config
from hal.motion import AxisPlan, build_chunks, plan_timeline, ramp_down_times, stoppable
from hal.mount import MountError, NotZeroed, PanTiltMount, SoftLimit

CFG = replace(load_config(), settle_ms=0)


def mount(time_scale: float = 0.0) -> tuple[PanTiltMount, SimBackend]:
    sim = SimBackend(time_scale=time_scale)
    m = PanTiltMount(CFG, sim)
    m.enable()
    return m, sim


def edges(sim: SimBackend, gpio: int) -> int:
    return sim.rising_edges.get(gpio, 0)


# -- motion planning ------------------------------------------------------- #


def test_ramp_down_length_and_shape():
    f, f0, a = 400.0, 20.0, 400.0
    t = ramp_down_times(f, f0, a)
    assert len(t) == int((f * f - f0 * f0) / (2 * a))
    gaps = [b - a_ for a_, b in zip([0.0] + t, t)]
    assert all(g2 > g1 for g1, g2 in zip(gaps, gaps[1:]))  # slowing down
    assert ramp_down_times(10.0, 20.0, a) == []            # already slow
    assert ramp_down_times(f, f0, 0.0) == []                # no accel limit: stop dead


def test_stoppable_without_stop_is_the_plan():
    pan, tilt = AxisPlan(500, 400, 400, 10), AxisPlan(-80, 150, 150, 5)
    events = plan_timeline(pan, tilt)
    assert list(stoppable(events, pan, tilt, lambda: False)) == events


def test_stoppable_stops_early_and_ramps():
    pan, tilt = AxisPlan(5000, 400, 400, 10), AxisPlan(0, 150, 150, 5)
    events = plan_timeline(pan, tilt)
    n = 0

    def should_stop() -> bool:
        return n >= 1500  # well into the cruise

    out = []
    for ev in stoppable(events, pan, tilt, should_stop):
        out.append(ev)
        n += 1
    times = [t for t, _ in out]
    assert 1500 < len(out) < 5000
    assert all(b >= a for a, b in zip(times, times[1:]))
    tail = [b - a for a, b in zip(times[1499:], times[1500:])]
    assert all(g2 >= g1 for g1, g2 in zip(tail, tail[1:]))  # monotonic ramp-down


def test_stop_before_start_moves_nothing():
    pan, tilt = AxisPlan(1000, 400, 400, 10), AxisPlan(0, 150, 150, 5)
    assert list(stoppable(plan_timeline(pan, tilt), pan, tilt, lambda: True)) == []


def test_chunks_capped_in_time():
    pan, tilt = AxisPlan(3000, 400, 400, 10), AxisPlan(300, 150, 150, 5)
    events = plan_timeline(pan, tilt)
    chunks = list(build_chunks(iter(events), 17, 22, 5, 12000, max_chunk_us=100_000))
    assert sum(c.pan_steps for c in chunks) == 3000
    assert sum(c.tilt_steps for c in chunks) == 300
    slowest_gap_us = 1e6 / 10
    assert all(c.duration_us <= 100_000 + slowest_gap_us for c in chunks)
    assert abs(sum(c.duration_us for c in chunks) / 1e6 - events[-1][0]) < 2e-3


# -- mount ------------------------------------------------------------------- #


def test_jog_runs_to_the_soft_limit():
    m, sim = mount()
    m.set_zero()
    r = m.jog("pan", 1)
    assert not r.stopped
    assert m.position()[0] == pytest.approx(CFG.pan.max_deg, abs=CFG.pan.deg_per_step)
    with pytest.raises(SoftLimit):
        m.jog("pan", 1)


def test_unzeroed_jog_is_capped():
    m, _ = mount()
    m.jog("tilt", -1)
    assert m.position()[1] == pytest.approx(-CFG.unzeroed_jog_deg, abs=CFG.tilt.deg_per_step)
    with pytest.raises(NotZeroed):
        m.move_to(0, 0)


def test_jog_needs_drivers():
    m = PanTiltMount(CFG, SimBackend())
    with pytest.raises(MountError):
        m.jog("pan", 1)


def test_jog_stop_keeps_position_exact():
    m, sim = mount(time_scale=0.2)
    m.set_zero()
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    r = m.jog("pan", -1, stop=stop)
    assert r.stopped and m.position_certain
    steps, _ = m.position_steps()
    assert steps == -edges(sim, CFG.pins.pan_step)  # every emitted edge accounted for
    assert CFG.pan.min_deg < r.pan_deg < 0


def test_stop_set_before_the_move_is_not_lost():
    m, sim = mount(time_scale=0.2)
    m.set_zero()
    stop = threading.Event()
    stop.set()
    r = m.move_to(30, 0, stop=stop)
    assert r.stopped and m.position() == (0.0, 0.0)


def test_stop_during_goto():
    m, _ = mount(time_scale=0.2)
    m.set_zero()
    threading.Timer(0.3, m.stop).start()
    r = m.move_to(60, 0)
    assert r.stopped and m.position_certain and 0 < r.pan_deg < 60


def test_speed_scales_duration():
    m, _ = mount(time_scale=0.05)
    m.set_zero()
    t0 = time.monotonic()
    m.move_to(20, 0)
    full = time.monotonic() - t0
    t0 = time.monotonic()
    m.move_to(40, 0, speed=0.5)
    assert time.monotonic() - t0 > 1.3 * full
    with pytest.raises(ValueError):
        m.move_to(0, 0, speed=1.5)


def test_backlash_tracking():
    m, sim = mount()
    m.set_zero()
    margin = CFG.pan.deg_to_steps(CFG.pan.backlash_deg)
    m.move_to(10, 0)
    assert m.status()["backlash_ok"]["pan"]
    m.jog_steps(-100, 0)  # leaves pan approached from the wrong side
    assert not m.status()["backlash_ok"]["pan"]
    at = m.position_steps()

    before = edges(sim, CFG.pins.pan_step)
    assert m.take_up_backlash()
    assert edges(sim, CFG.pins.pan_step) - before == 2 * margin
    assert m.position_steps() == at and m.status()["backlash_ok"]["pan"]
    assert not m.take_up_backlash()  # nothing left to do

    # An absolute move to where the head already is: no-op when the lash is right,
    # back off and return when a jog left it the other way.
    before = edges(sim, CFG.pins.pan_step)
    m.move_to(*m.position())
    assert edges(sim, CFG.pins.pan_step) == before
    m.jog_steps(-10, 0)
    r = m.move_to(*m.position())
    assert r.backlash_stage and edges(sim, CFG.pins.pan_step) - before == 10 + 2 * margin


def test_nudge_ends_with_backlash_taken_up():
    m, _ = mount()
    m.nudge(pan_deg=-1.0)           # no datum: relative, then take-up
    assert m.status()["backlash_ok"]["pan"]
    with pytest.raises(SoftLimit):
        m.check_nudge(CFG.unzeroed_jog_deg + 1, 0)
    m.set_zero()
    r = m.nudge(pan_deg=-1.0)       # with a datum: move_by, two-stage
    assert r.backlash_stage and m.status()["backlash_ok"]["pan"]
    assert r.pan_deg == pytest.approx(-1.0, abs=CFG.pan.deg_per_step)
