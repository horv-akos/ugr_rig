#!/usr/bin/env python3
"""Bench control and measurement for the pan-tilt head.

Run from the repository root:

    python -m tools.mountctl --backend sim selftest
    python -m tools.mountctl info
    python -m tools.mountctl goto 30 -10
    python -m tools.mountctl hold

The measurement subcommands (ratio, backlash, repeat, limits) are the
procedures in docs/PIVOT_DETAIL.md §8. They prompt for digital angle gauge
readings and append to a CSV so the numbers end up somewhere other than a
notebook.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import logging
import signal
import statistics
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hal.backend import SimBackend, create_backend  # noqa: E402
from hal.config import MountConfig, load_config  # noqa: E402
from hal.motion import AxisPlan, build_chunks, plan_timeline, step_times  # noqa: E402
from hal.mount import (CHUNK_US, MountError, PanTiltMount,  # noqa: E402
                       install_signal_handlers)
from hal import tmc2209  # noqa: E402

LOG_DIR = Path("measurements")
HOLD_CHECK_S = 1.0     # between driver checks while holding
HOLD_HELP = "afterwards, hold until Ctrl-C is pressed twice"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def open_mount(args, cfg: MountConfig | None = None) -> PanTiltMount:
    cfg = cfg or load_config(args.config)
    kwargs = {}
    if args.backend == "sim":
        kwargs["time_scale"] = args.time_scale
    drivers = tmc2209.create_drivers("uart" if args.backend == "pigpio" else "sim",
                                     cfg.drivers, (cfg.pan, cfg.tilt))
    mount = PanTiltMount(cfg, create_backend(args.backend, **kwargs), drivers)
    install_signal_handlers(mount)
    return mount


def ask_float(prompt: str) -> float:
    while True:
        raw = input(f"{prompt}: ").strip()
        try:
            return float(raw)
        except ValueError:
            print("  need a number")


def log_rows(name: str, fieldnames: list[str], rows: list[dict]) -> Path:
    LOG_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = LOG_DIR / f"{name}_{stamp}.csv"
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return path


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


def cmd_info(args) -> int:
    cfg = load_config(args.config)
    print(f"config: {cfg.source}")
    print(f"pins (BCM): {dataclasses.asdict(cfg.pins)}")
    print(f"settle {cfg.settle_ms} ms, step pulse {cfg.pulse_width_us} us")
    for axis in (cfg.pan, cfg.tilt):
        print(
            f"\n{axis.name}\n"
            f"  gearing      {axis.gear_label} = {axis.gear_ratio:.4f}\n"
            f"  resolution   {axis.steps_per_deg:.3f} steps/deg"
            f"  ({axis.deg_per_step*1000:.2f} mdeg/step)\n"
            f"  soft limits  {axis.min_deg:+.1f} .. {axis.max_deg:+.1f} deg\n"
            f"  speed        {axis.max_speed_deg_s} deg/s,"
            f" accel {axis.accel_deg_s2} deg/s^2\n"
            f"  backlash     {axis.backlash_deg} deg,"
            f" approach {'+' if axis.approach_sign > 0 else '-'}"
        )

    # Waveform cost of the worst-case move, so the DMA budget is visible.
    span_pan = cfg.pan.deg_to_steps(cfg.pan.max_deg - cfg.pan.min_deg)
    span_tilt = cfg.tilt.deg_to_steps(cfg.tilt.max_deg - cfg.tilt.min_deg)
    plans = (
        AxisPlan(span_pan, cfg.pan.max_speed_deg_s * cfg.pan.steps_per_deg,
                 cfg.pan.accel_deg_s2 * cfg.pan.steps_per_deg,
                 cfg.pan.start_speed_deg_s * cfg.pan.steps_per_deg),
        AxisPlan(span_tilt, cfg.tilt.max_speed_deg_s * cfg.tilt.steps_per_deg,
                 cfg.tilt.accel_deg_s2 * cfg.tilt.steps_per_deg,
                 cfg.tilt.start_speed_deg_s * cfg.tilt.steps_per_deg),
    )
    events = plan_timeline(*plans)
    chunks = list(build_chunks(events, cfg.pins.pan_step, cfg.pins.tilt_step,
                               cfg.pulse_width_us, 12000, CHUNK_US))
    print(
        f"\nfull-travel move: {span_pan} + {span_tilt} steps, "
        f"{events[-1][0]:.1f} s, {len(chunks)} chained waves of at most {CHUNK_US / 1000:g} ms"
    )
    print(f"jog or nudge before a zero is set: at most {cfg.unzeroed_jog_deg:g} deg at a time")
    return 0


def cmd_drivers(args) -> int:
    """Configure both TMC2209s over UART, read them back, and print what they hold. The
    drivers stay disabled. First thing to run after wiring, with the motor rail on."""
    cfg = load_config(args.config)
    bus = tmc2209.create_drivers("uart" if args.backend == "pigpio" else "sim",
                                 cfg.drivers, (cfg.pan, cfg.tilt))
    try:
        report = bus.configure()
        warnings = bus.check()
    except tmc2209.DriverError as exc:
        print(f"FAILED: {exc}")
        return 1
    finally:
        bus.close()
    print(f"port {cfg.drivers.port} at {cfg.drivers.baud} baud, Rsense {cfg.drivers.rsense_ohm} ohm")
    for name, r in report.items():
        print(f"  {name:5s} address {r['address']}  1/{r['microsteps']}  "
              f"run {r['run_current_a']:.3f} A (IRUN {r['irun']})  "
              f"hold {r['hold_current_a']:.3f} A (IHOLD {r['ihold']})  vsense {r['vsense']}  "
              f"GCONF {r['gconf']}  CHOPCONF {r['chopconf']}")
    for w in warnings:
        print(f"  warning: {w}")
    print("verified: written, read back, write counter matches")
    return 0


def cmd_status(args) -> int:
    with open_mount(args) as mount:
        print(mount.status())
    return 0


def cmd_goto(args) -> int:
    with open_mount(args) as mount:
        mount.enable()
        mount.set_zero(args.assume_pan, args.assume_tilt)
        result = mount.move_to(args.pan, args.tilt)
        print(
            f"at {result.pan_deg:+.4f}, {result.tilt_deg:+.4f} deg "
            f"({result.motion_s:.2f} s motion"
            f"{', two-stage approach' if result.backlash_stage else ''})"
        )
        if args.hold:
            return hold_until_released(mount)
    return 0


def cmd_jog(args) -> int:
    """Relative move with no datum and no limit check - for first power-on."""
    cfg = load_config(args.config)
    with open_mount(args) as mount:
        mount.enable()
        pan_steps = cfg.pan.deg_to_steps(args.pan)
        tilt_steps = cfg.tilt.deg_to_steps(args.tilt)
        print(f"jogging {pan_steps} pan / {tilt_steps} tilt steps")
        mount.jog_steps(pan_steps, tilt_steps)
        if args.hold:
            return hold_until_released(mount)
    return 0


def hold_loop(mount: PanTiltMount, interval_s: float, released: Callable[[], bool],
              poll_s: float = 0.2) -> int:
    """Keep an enabled mount energised until `released()`, checking the drivers every
    `interval_s`. Letting go drops the camera, so a problem is reported and the hold goes on;
    it ends early only when the mount itself released the motors because their configuration
    is no longer proven. Returns how many problems were seen (a lasting one counts once)."""
    problems = 0
    last = None
    due = time.monotonic() + interval_s
    while not released():
        time.sleep(poll_s)
        if time.monotonic() < due:
            continue
        due = time.monotonic() + interval_s
        try:
            warnings = mount.check_drivers()
            said = "warning: " + "; ".join(warnings) if warnings else None
        except (MountError, OSError) as exc:      # OSError: pyserial, the port itself failed
            said = f"PROBLEM: {exc}"
            # every reset is its own event: the motors let go each time
            if said != last or isinstance(exc, tmc2209.DriverReset):
                problems += 1
                last = None
        if said != last:
            print(f"{datetime.now():%H:%M:%S}  {said or 'drivers OK again'}", flush=True)
            last = said
        if not mount.enabled:
            print("MOTORS RELEASED: support the camera", flush=True)
            break
    return problems


def hold_until_released(mount: PanTiltMount, interval_s: float = HOLD_CHECK_S) -> int:
    """Keep an enabled mount energised until Ctrl-C has been pressed twice: the tilt gears
    back-drive, so letting go drops an unbalanced camera. The caller releases, by closing the
    mount. Exit code: 1 if the drivers reported a problem meanwhile."""
    presses = 0
    asked = False

    def on_sigint(signum, frame) -> None:
        nonlocal presses
        presses += 1                  # only count: a UART exchange may be under way

    def released() -> bool:
        nonlocal asked
        if presses == 1 and not asked:
            asked = True
            print("\nsupport the camera, then press Ctrl-C again to release", flush=True)
        return presses >= 2

    previous = signal.signal(signal.SIGINT, on_sigint)
    try:
        for name, r in mount.drivers.report.items():
            print(f"  {name:5s} holding at {r['hold_current_a']:.3f} A")
        print("holding. Ctrl-C twice to release", flush=True)
        problems = hold_loop(mount, interval_s, released)
    finally:
        signal.signal(signal.SIGINT, previous)
    print(f"releasing{f', {problems} problem(s) seen' if problems else ''}", flush=True)
    return 1 if problems else 0


def cmd_hold(args) -> int:
    """Energise both axes where they stand and hold them. No steps are sent and no zero is
    set."""
    if args.interval <= 0:
        print("--interval must be above 0")
        return 2
    with open_mount(args) as mount:
        try:
            mount.enable()
        except MountError as exc:
            print(f"FAILED, motors off: {exc}")
            return 1
        return hold_until_released(mount, args.interval)


def cmd_ratio(args) -> int:
    """§8.1 - verify the gear ratio against the angle gauge.

    Commands a nominal angle, asks what actually happened, and prints the
    corrected ratio. Do this on tilt before trusting any commanded angle.
    """
    cfg = load_config(args.config)
    axis = cfg.axis(args.axis)
    with open_mount(args) as mount:
        mount.enable()
        mount.set_zero()
        print(f"\nzero the gauge on the {axis.name} axis now.")
        input("press enter when zeroed ")

        target = args.deg if args.axis == "pan" else 0.0
        mount.move_to(args.deg if args.axis == "pan" else 0.0,
                      args.deg if args.axis == "tilt" else 0.0)
        measured = ask_float(f"gauge reading after a commanded {args.deg:+.2f} deg")

        if measured == 0:
            print("measured zero - nothing moved, check wiring and Vref")
            if args.hold:
                hold_until_released(mount)
            return 1
        true_ratio = axis.gear_ratio * args.deg / measured
        true_spd = axis.motor_steps_per_rev * axis.microsteps * true_ratio / 360.0
        err = 100.0 * (measured - args.deg) / args.deg
        print(
            f"\ncommanded {args.deg:+.3f} deg, measured {measured:+.3f} deg ({err:+.2f}%)\n"
            f"configured ratio {axis.gear_ratio:.5f} -> measured {true_ratio:.5f}\n"
            f"steps/deg       {axis.steps_per_deg:.4f} -> {true_spd:.4f}"
        )
        for teeth, label in ((64 / 21, "64:21 herringbone"), (123 / 16, "123:16 belt")):
            if abs(true_ratio - teeth) / teeth < 0.05:
                print(f"within 5% of {label} ({teeth:.5f})")
        print(
            f"\nto use the measured value, in [axis.{axis.name}] of {cfg.source}:\n"
            f"  gear_ratio = {true_ratio:.6f}\n"
            f"and comment out gear_teeth_driver / gear_teeth_driven"
        )
        if args.hold:
            return hold_until_released(mount)
    return 0


def cmd_backlash(args) -> int:
    """§8.3 - approach one target from both directions, measure the difference.

    Backlash compensation is switched off for the test, otherwise it hides the
    thing being measured.
    """
    axis_name = args.axis
    raw = load_config(args.config)
    patched = dataclasses.replace(
        raw,
        pan=dataclasses.replace(raw.pan, backlash_deg=0.0),
        tilt=dataclasses.replace(raw.tilt, backlash_deg=0.0),
    )
    rows = []
    with open_mount(args, patched) as mount:
        mount.enable()
        mount.set_zero()
        print(f"\nzero the gauge on {axis_name} at 0 deg.")
        input("press enter when zeroed ")

        def go(target: float) -> None:
            mount.move_to(target if axis_name == "pan" else 0.0,
                          target if axis_name == "tilt" else 0.0)

        for i in range(args.repeats):
            go(args.at - args.swing)
            go(args.at)
            from_below = ask_float(f"[{i+1}] reading arriving from below")
            go(args.at + args.swing)
            go(args.at)
            from_above = ask_float(f"[{i+1}] reading arriving from above")
            rows.append(
                {
                    "cycle": i + 1,
                    "target_deg": args.at,
                    "from_below_deg": from_below,
                    "from_above_deg": from_above,
                    "backlash_deg": round(from_above - from_below, 4),
                }
            )
            print(f"    backlash {from_above - from_below:+.4f} deg")

        values = [abs(r["backlash_deg"]) for r in rows]
        mean = statistics.fmean(values)
        print(f"\nmean backlash {mean:.4f} deg over {len(values)} cycles")
        if len(values) > 1:
            print(f"spread {min(values):.4f} .. {max(values):.4f} deg")
        print(f"suggested config: backlash_deg = {mean * 1.5:.3f}  (1.5x mean)")
        path = log_rows(f"backlash_{axis_name}", list(rows[0]), rows)
        print(f"logged to {path}")
        if args.hold:
            return hold_until_released(mount)
    return 0


def cmd_repeat(args) -> int:
    """§8.4 - angular repeatability. zero -> move -> return -> re-measure, N times."""
    rows = []
    with open_mount(args) as mount:
        mount.enable()
        mount.set_zero()
        print("\nzero the gauge at the home pose.")
        input("press enter when zeroed ")
        for i in range(args.n):
            mount.move_to(args.pan, args.tilt)
            at_target = ask_float(f"[{i+1}] reading at target")
            mount.move_to(0.0, 0.0)
            back_home = ask_float(f"[{i+1}] reading back at home")
            rows.append(
                {
                    "cycle": i + 1,
                    "target_reading_deg": at_target,
                    "home_reading_deg": back_home,
                    "return_error_deg": round(back_home, 4),
                }
            )

        targets = [r["target_reading_deg"] for r in rows]
        homes = [r["home_reading_deg"] for r in rows]
        print(
            f"\ntarget spread {max(targets)-min(targets):.4f} deg"
            f"  (sd {statistics.pstdev(targets):.4f})\n"
            f"home   spread {max(homes)-min(homes):.4f} deg"
            f"  (sd {statistics.pstdev(homes):.4f})\n"
            "this is the floor on session-to-session comparability - put it in the report"
        )
        path = log_rows("repeatability", list(rows[0]), rows)
        print(f"logged to {path}")
        if args.hold:
            return hold_until_released(mount)
    return 0


def cmd_limits(args) -> int:
    """§8.5 - find the mechanical interference points by hand, before any
    automated move. Drivers stay disabled so the head moves freely."""
    cfg = load_config(args.config)
    print(
        "\nDrivers stay off for this. Move each axis by hand to where it fouls,\n"
        "back off a safe margin, and read the gauge.\n"
    )
    out = {}
    for axis in (cfg.pan, cfg.tilt):
        lo = ask_float(f"{axis.name} safe minimum (deg)")
        hi = ask_float(f"{axis.name} safe maximum (deg)")
        out[axis.name] = (lo, hi)
    print("\nput these into config/mount.toml:\n")
    for name, (lo, hi) in out.items():
        print(f"[axis.{name}]\nmin_deg = {lo}\nmax_deg = {hi}\n")
    return 0


def cmd_selftest(args) -> int:
    """Exercise the step maths, backlash logic and wave chunking against the
    simulator. No hardware, no daemon."""
    cfg = load_config(args.config)
    failures = []

    def check(label, cond, detail=""):
        print(f"  {'ok  ' if cond else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
        if not cond:
            failures.append(label)

    print("profile")
    times = step_times(1000, f_max=400.0, f_start=10.0, accel=400.0)
    check("monotonic", all(b > a for a, b in zip(times, times[1:])))
    mid = len(times) // 2
    check(
        "accelerates then cruises",
        (times[1] - times[0]) > (times[mid] - times[mid - 1]),
        f"first gap {1e3*(times[1]-times[0]):.2f} ms, cruise gap "
        f"{1e3*(times[mid]-times[mid-1]):.2f} ms",
    )
    check("symmetric ramp", abs(times[0] - (times[-1] - times[-2])) < 1e-9)
    short = step_times(20, f_max=4000.0, f_start=10.0, accel=100.0)
    check("triangular when short", len(short) == 20 and short[-1] > 0)

    print("timeline")
    pan = AxisPlan(1000, 400.0, 400.0, 10.0)
    tilt = AxisPlan(100, 150.0, 150.0, 5.0)
    events = plan_timeline(pan, tilt)
    check("all steps present", len(events) == 1100)
    check("sorted", all(b[0] >= a[0] for a, b in zip(events, events[1:])))
    pan_end = max(t for t, ax in events if ax == 0)
    tilt_end = max(t for t, ax in events if ax == 1)
    check("axes finish together", abs(pan_end - tilt_end) < 1e-6,
          f"{pan_end:.3f} vs {tilt_end:.3f} s")

    print("waveform")
    chunks = list(build_chunks(events, 17, 22, cfg.pulse_width_us, 600))
    total_pan = sum(c.pan_steps for c in chunks)
    total_tilt = sum(c.tilt_steps for c in chunks)
    check("chunks conserve steps", (total_pan, total_tilt) == (1000, 100))
    check("no chunk over the cap", all(len(c.pulses) <= 600 for c in chunks))
    seam = sum(c.duration_us for c in chunks) / 1e6
    check("chained duration matches plan", abs(seam - events[-1][0]) < 2e-3,
          f"{seam:.4f} s vs {events[-1][0]:.4f} s")

    print("mount")
    sim = SimBackend(time_scale=0.0)
    with PanTiltMount(cfg, sim) as mount:
        try:
            mount.enable()
            mount.move_to(1.0, 1.0)
            check("refuses to move before zeroing", False)
        except Exception as exc:
            check("refuses to move before zeroing", "NotZeroed" in type(exc).__name__)

        mount.set_zero()
        try:
            mount.move_to(cfg.pan.max_deg + 10, 0.0)
            check("soft limit enforced", False)
        except Exception as exc:
            check("soft limit enforced", "SoftLimit" in type(exc).__name__)

        before = sim.rising_edges.get(cfg.pins.pan_step, 0)
        mount.move_to(10.0, 5.0)
        pan_deg, tilt_deg = mount.position()
        emitted = sim.rising_edges.get(cfg.pins.pan_step, 0) - before
        check("arrives at target", abs(pan_deg - 10.0) < cfg.pan.deg_per_step
              and abs(tilt_deg - 5.0) < cfg.tilt.deg_per_step,
              f"{pan_deg:.4f}, {tilt_deg:.4f} deg")
        check("pan edge count matches", emitted == cfg.pan.deg_to_steps(10.0),
              f"{emitted} edges")

        # Direction sense: the DIR pin in force at each STEP edge.
        dir_levels = {
            lv[cfg.pins.pan_dir]
            for _, gpio, lv in sim.edge_log
            if gpio == cfg.pins.pan_step
        }
        check("pan DIR held at the positive level (see invert)",
              dir_levels == {0 if cfg.pan.invert else 1}, f"{dir_levels}")

        # Reversing must cost extra steps: overshoot past the target, come back.
        start_steps, _ = mount.position_steps()
        before = sim.rising_edges.get(cfg.pins.pan_step, 0)
        mount.move_to(5.0, 5.0)
        emitted = sim.rising_edges.get(cfg.pins.pan_step, 0) - before
        margin = cfg.pan.deg_to_steps(cfg.pan.backlash_deg)
        direct = abs(cfg.pan.deg_to_steps(5.0) - start_steps)
        check("backlash: reversing overshoots and returns",
              emitted == direct + 2 * margin,
              f"{emitted} edges vs {direct} direct + 2x{margin} margin")

        # Moving further in the approach direction must not trigger a two-stage
        # move - the head already arrives from the correct side.
        before = sim.rising_edges.get(cfg.pins.pan_step, 0)
        res = mount.move_to(20.0, 5.0)
        emitted = sim.rising_edges.get(cfg.pins.pan_step, 0) - before
        expected = cfg.pan.deg_to_steps(20.0) - cfg.pan.deg_to_steps(5.0)
        check("no backlash stage when already approaching correctly",
              not res.backlash_stage and emitted == expected,
              f"{emitted} edges")

        mount.disable()
        check("disable drops the datum", not mount.is_zeroed)
        pins = cfg.pins
        check("enable line released", sim.levels[pins.enable] == 1)

    print("jog")
    import threading as _t
    sim3 = SimBackend(time_scale=0.0)
    with PanTiltMount(cfg, sim3) as mount:
        mount.enable()
        mount.jog("pan", 1)
        pan_deg, _ = mount.position()
        check("without a zero a jog is capped", abs(pan_deg - cfg.unzeroed_jog_deg)
              < cfg.pan.deg_per_step, f"{pan_deg:.3f} deg, cap {cfg.unzeroed_jog_deg:g}")
        mount.set_zero()
        mount.jog("tilt", 1)
        _, tilt_deg = mount.position()
        check("with a zero a jog stops on the soft limit",
              abs(tilt_deg - cfg.tilt.max_deg) < cfg.tilt.deg_per_step, f"{tilt_deg:.3f} deg")
        mount.move_to(0.0, 0.0, settle=False)
        mount.jog_steps(-200, 0)
        check("moving against approach_sign is remembered",
              not mount.status()["backlash_ok"]["pan"])
        at = mount.position_steps()
        before = sim3.rising_edges.get(cfg.pins.pan_step, 0)
        mount.take_up_backlash()
        margin = cfg.pan.deg_to_steps(cfg.pan.backlash_deg)
        emitted = sim3.rising_edges.get(cfg.pins.pan_step, 0) - before
        check("take_up_backlash: back off and return, position unchanged",
              mount.position_steps() == at and emitted == 2 * margin
              and mount.status()["backlash_ok"]["pan"], f"{emitted} edges, margin {margin}")

    sim4 = SimBackend(time_scale=1.0)  # real time, so the stop lands mid-jog
    with PanTiltMount(cfg, sim4) as mount:
        mount.enable()
        mount.set_zero()
        stop = _t.Event()
        _t.Timer(0.5, stop.set).start()
        t0 = time.monotonic()
        res = mount.jog("pan", 1, stop=stop)
        steps, _ = mount.position_steps()
        check("stop ramps a jog down", res.stopped and mount.position_certain,
              f"{res.pan_deg:.3f} deg, {time.monotonic() - t0:.2f} s")
        check("every emitted step accounted for",
              steps == sim4.rising_edges.get(cfg.pins.pan_step, 0), f"{steps} steps")

    print("drivers (TMC2209 over UART, simulated bus)")
    check("CRC matches the datasheet example (read GCONF, address 0)",
          tmc2209.read_datagram(0, tmc2209.GCONF).hex() == "05000048")
    bus = tmc2209.create_drivers("sim", cfg.drivers, (cfg.pan, cfg.tilt))
    sim_bus = bus.port
    check("unconfigured drivers run the strap microstepping",
          [d.microsteps for d in sim_bus.drivers.values()] == [8, 32])
    report = bus.configure()
    check("configured: both at the configured microstepping",
          all(d.microsteps == cfg.pan.microsteps for d in sim_bus.drivers.values()),
          f"1/{cfg.pan.microsteps}")
    check("run current within a step of the request",
          abs(report["pan"]["run_current_a"] - cfg.pan.run_current_a) < 0.04,
          f"{report['pan']['run_current_a']} A for {cfg.pan.run_current_a} A")
    with PanTiltMount(cfg, SimBackend(), bus) as mount:
        mount.enable()
        mount.set_zero()
        mount.move_to(2.0, 0.0, settle=False)
        sim_bus.power_cycle()                     # motor rail switched off and on
        try:
            mount.move_to(0.0, 0.0, settle=False)
            check("rail cycle caught before the next move", False)
        except tmc2209.DriverReset:
            check("rail cycle caught before the next move", True)
        check("position marked uncertain after a reset", not mount.position_certain)
        check("drivers reconfigured after the reset",
              all(d.microsteps == cfg.pan.microsteps for d in sim_bus.drivers.values()))
    bus = tmc2209.create_drivers("sim", cfg.drivers, (cfg.pan, cfg.tilt))
    sim5 = SimBackend()
    with PanTiltMount(cfg, sim5, bus) as mount:
        mount.enable()
        polls = iter(range(6))
        held: list[bool] = []

        def released() -> bool:
            i = next(polls)
            if i == 2:
                bus.port.power_cycle()            # motor rail dips while holding
            held.append(sim5.levels[cfg.pins.enable] == 0)
            return i == 5

        problems = hold_loop(mount, 0.0, released, poll_s=0.01)
        check("hold: stays energised through a rail dip, and reports it once",
              all(held) and len(held) == 6 and problems == 1)
        check("hold: sends no steps", not any(sim5.rising_edges.values()))
    silent = tmc2209.create_drivers("sim", cfg.drivers, (cfg.pan, cfg.tilt))
    silent.port.vmot = False
    m2 = PanTiltMount(cfg, SimBackend(), silent)
    try:
        m2.enable()
        check("motor rail off: enable refused", False)
    except tmc2209.DriverNoAnswer:
        check("motor rail off: enable refused", not m2.enabled)
    m2.close()
    deaf = tmc2209.create_drivers("sim", cfg.drivers, (cfg.pan, cfg.tilt))
    deaf.port.ignore_writes = True
    try:
        deaf.configure()
        check("writes that do not land are caught", False)
    except tmc2209.DriverMismatch as exc:
        check("writes that do not land are caught", True, str(exc)[:60])
    noisy = tmc2209.create_drivers("sim", cfg.drivers, (cfg.pan, cfg.tilt))
    noisy.port.corrupt_replies = 1
    noisy.configure()
    check("one corrupted reply is retried", noisy.configured)

    print("abort")
    sim2 = SimBackend(time_scale=1.0)  # run a move in real time so it can be cut
    with PanTiltMount(cfg, sim2) as mount:
        mount.enable()
        mount.set_zero()
        _t.Timer(0.4, mount.abort).start()
        t0 = time.monotonic()
        try:
            mount.move_to(40.0, 0.0, settle=False)
            check("abort stops the move", False)
        except Exception as exc:
            elapsed = time.monotonic() - t0
            check("abort stops the move", "MoveAborted" in type(exc).__name__
                  and elapsed < 3.0, f"after {elapsed:.2f} s of an ~9 s move")
        pan_deg, _ = mount.position()
        check("position is an estimate, not the target",
              0.0 < pan_deg < 40.0, f"{pan_deg:.3f} deg")
        check("position flagged uncertain", not mount.position_certain)

    print(f"\n{'all checks passed' if not failures else str(len(failures)) + ' FAILED'}")
    return 1 if failures else 0


# --------------------------------------------------------------------------- #


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="mountctl", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=None, help="path to mount.toml")
    p.add_argument("--backend", default="pigpio", choices=["pigpio", "sim"])
    p.add_argument("--time-scale", type=float, default=0.0,
                   help="sim only: 1.0 runs in real time, 0 runs instantly")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info", help="print derived constants and waveform cost").set_defaults(fn=cmd_info)
    sub.add_parser("status", help="current state").set_defaults(fn=cmd_status)
    sub.add_parser("drivers", help="configure the TMC2209s over UART and read them back "
                   "(drivers stay off)").set_defaults(fn=cmd_drivers)
    sub.add_parser("selftest", help="run the logic against the simulator").set_defaults(fn=cmd_selftest)
    sub.add_parser("limits", help="record soft limits by hand (§8.5)").set_defaults(fn=cmd_limits)

    g = sub.add_parser("goto", help="absolute move, assuming current pose is the datum")
    g.add_argument("pan", type=float)
    g.add_argument("tilt", type=float)
    g.add_argument("--assume-pan", type=float, default=0.0,
                   help="degrees the head is currently at")
    g.add_argument("--assume-tilt", type=float, default=0.0)
    g.add_argument("--hold", action="store_true", help=HOLD_HELP)
    g.set_defaults(fn=cmd_goto)

    j = sub.add_parser("jog", help="relative move, no datum, no limit check")
    j.add_argument("--pan", type=float, default=0.0)
    j.add_argument("--tilt", type=float, default=0.0)
    j.add_argument("--hold", action="store_true", help=HOLD_HELP)
    j.set_defaults(fn=cmd_jog)

    h = sub.add_parser("hold", help="energise and hold until Ctrl-C is pressed twice")
    h.add_argument("--interval", type=float, default=HOLD_CHECK_S,
                   help="seconds between driver checks over UART")
    h.set_defaults(fn=cmd_hold)

    r = sub.add_parser("ratio", help="verify a gear ratio against the angle gauge (§8.1)")
    r.add_argument("--axis", choices=["pan", "tilt"], required=True)
    r.add_argument("--deg", type=float, default=30.0)
    r.add_argument("--hold", action="store_true", help=HOLD_HELP)
    r.set_defaults(fn=cmd_ratio)

    b = sub.add_parser("backlash", help="measure backlash (§8.3)")
    b.add_argument("--axis", choices=["pan", "tilt"], required=True)
    b.add_argument("--at", type=float, default=0.0, help="target angle")
    b.add_argument("--swing", type=float, default=5.0, help="approach distance")
    b.add_argument("--repeats", type=int, default=3)
    b.add_argument("--hold", action="store_true", help=HOLD_HELP)
    b.set_defaults(fn=cmd_backlash)

    rep = sub.add_parser("repeat", help="angular repeatability (§8.4)")
    rep.add_argument("--pan", type=float, default=30.0)
    rep.add_argument("--tilt", type=float, default=15.0)
    rep.add_argument("-n", type=int, default=10)
    rep.add_argument("--hold", action="store_true", help=HOLD_HELP)
    rep.set_defaults(fn=cmd_repeat)

    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
