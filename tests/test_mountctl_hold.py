"""`mountctl hold`: the motors stay on until the operator says so twice."""

from __future__ import annotations

import signal
import threading
from dataclasses import replace

import pytest

from hal import tmc2209 as t
from hal.backend import SimBackend
from hal.config import load_config
from hal.mount import PanTiltMount
from tools import mountctl

CFG = replace(load_config(), settle_ms=0)


@pytest.fixture
def rig(monkeypatch):
    """The mount `mountctl` will open, its pulse simulator and its driver bus."""
    sim = SimBackend()
    bus = t.create_drivers("sim", CFG.drivers, (CFG.pan, CFG.tilt))
    mount = PanTiltMount(CFG, sim, bus)
    monkeypatch.setattr(mountctl, "open_mount", lambda args: mount)
    yield mount, sim, bus
    mount.close()


def energised(sim: SimBackend) -> bool:
    return sim.levels[CFG.pins.enable] == 0


def run_hold(mount, steps: dict, end: int) -> int:
    """hold_loop with `steps[i]` called at poll i, released at poll `end`."""
    n = -1

    def released() -> bool:
        nonlocal n
        n += 1
        if n in steps:
            steps[n]()
        return n >= end

    return mountctl.hold_loop(mount, 0.0, released, poll_s=0.0)


def test_holds_until_the_second_ctrl_c(rig, capsys):
    mount, sim, _ = rig
    before = signal.getsignal(signal.SIGINT)
    seen = []
    timers = [
        threading.Timer(0.5, lambda: seen.append(energised(sim))),
        threading.Timer(0.6, lambda: signal.raise_signal(signal.SIGINT)),
        threading.Timer(1.3, lambda: seen.append(energised(sim))),     # still on after one
        threading.Timer(1.4, lambda: signal.raise_signal(signal.SIGINT)),
    ]
    for timer in timers:
        timer.start()
    assert mountctl.main(["--backend", "sim", "hold", "--interval", "0.1"]) == 0
    out = capsys.readouterr().out
    assert seen == [True, True]
    assert "support the camera" in out and "tilt  holding at 0." in out
    assert not energised(sim) and not any(sim.rising_edges.values())
    assert signal.getsignal(signal.SIGINT) is before


def test_enable_refused_is_reported(rig, capsys):
    _, sim, bus = rig
    bus.port.vmot = False
    assert mountctl.main(["--backend", "sim", "hold"]) == 1
    assert "FAILED" in capsys.readouterr().out and not energised(sim)


def test_bad_interval_never_opens_the_mount(monkeypatch):
    monkeypatch.setattr(mountctl, "open_mount", lambda args: pytest.fail("opened"))
    assert mountctl.main(["--backend", "sim", "hold", "--interval", "0"]) == 2


def test_silence_and_resets_keep_the_hold(rig, capsys):
    mount, sim, bus = rig
    mount.enable()

    def rail(on: bool) -> None:
        bus.port.vmot = on
        if on:
            bus.port.power_cycle()

    problems = run_hold(mount, {2: lambda: rail(False), 5: lambda: rail(True),
                                8: bus.port.power_cycle}, 12)
    assert problems == 3                       # silence once, then two resets
    assert energised(sim) and mount.enabled
    assert [d.microsteps for d in bus.port.drivers.values()] == [16, 16]
    out = capsys.readouterr().out
    assert out.count("does not answer") == 1 and out.count("driver reset") == 2
    assert "OK again" in out


def test_resets_at_consecutive_checks_are_both_counted(rig):
    mount, _, bus = rig
    mount.enable()
    assert run_hold(mount, {1: bus.port.power_cycle, 2: bus.port.power_cycle}, 5) == 2


def test_unprovable_configuration_ends_the_hold(rig, capsys):
    mount, sim, bus = rig
    mount.enable()

    def reset_deaf() -> None:
        bus.port.power_cycle()
        bus.port.ignore_writes = True

    assert run_hold(mount, {2: reset_deaf}, 10_000) == 1
    assert not mount.enabled and not energised(sim)
    assert "MOTORS RELEASED" in capsys.readouterr().out


def test_a_serial_port_error_keeps_the_hold(rig, monkeypatch, capsys):
    mount, sim, bus = rig
    mount.enable()

    def broken(data: bytes) -> None:
        raise OSError("port failed")

    assert run_hold(mount, {2: lambda: monkeypatch.setattr(bus.port, "write", broken)}, 6) == 1
    assert energised(sim) and "port failed" in capsys.readouterr().out


def test_sigterm_releases_at_once(rig, monkeypatch):
    """Its handler closes the backend, which then takes no more writes."""
    mount, sim, _ = rig
    monkeypatch.setattr(sim, "close", lambda: setattr(sim, "write", None))

    def fake_open(args):
        mountctl.install_signal_handlers(mount)
        return mount

    monkeypatch.setattr(mountctl, "open_mount", fake_open)
    saved = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
    threading.Timer(0.5, lambda: signal.raise_signal(signal.SIGTERM)).start()
    try:
        with pytest.raises(SystemExit):
            mountctl.main(["--backend", "sim", "hold"])
    finally:
        signal.signal(signal.SIGTERM, saved[0])
        signal.signal(signal.SIGINT, saved[1])
    assert not energised(sim)


def test_warnings_are_printed_once_and_are_not_problems(rig, capsys):
    mount, _, bus = rig
    mount.enable()

    def hot() -> None:
        bus.port.drivers[1].regs[t.DRV_STATUS] = t.OTPW

    assert run_hold(mount, {1: hot}, 8) == 0
    assert capsys.readouterr().out.count("pre-warning") == 1


@pytest.mark.parametrize("argv,answers", [
    (["goto", "1", "2"], []),
    (["jog", "--tilt", "1"], []),
    (["ratio", "--axis", "tilt", "--deg", "2"], ["", "2"]),
    (["ratio", "--axis", "tilt", "--deg", "2"], ["", "0"]),        # nothing moved: still held
    (["backlash", "--axis", "tilt", "--repeats", "1"], ["", "0", "0.1"]),
    (["repeat", "-n", "1", "--pan", "1", "--tilt", "1"], ["", "1", "0"]),
])
@pytest.mark.parametrize("hold", [True, False])
def test_hold_flag_keeps_the_motors_on_after_the_command(argv, answers, hold, monkeypatch,
                                                         tmp_path):
    """The real open_mount: closing the mount used to release whatever --hold said, and
    backlash opened it without a driver bus, which real hardware refuses to enable."""
    opened, held = [], []
    real_open = mountctl.open_mount

    def spy_open(args, cfg=None):
        opened.append(real_open(args, cfg))
        return opened[-1]

    def spy_hold(mount, interval_s=1.0):
        held.append((mount.enabled, len(list(tmp_path.glob("measurements/*.csv")))))
        return 0

    saved = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    given = iter(answers)
    monkeypatch.setattr(mountctl, "open_mount", spy_open)
    monkeypatch.setattr(mountctl, "hold_until_released", spy_hold)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(given))
    monkeypatch.chdir(tmp_path)
    try:
        code = mountctl.main(["--backend", "sim", *argv, *(["--hold"] if hold else [])])
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)
    assert code == (1 if answers[-1:] == ["0"] and argv[0] == "ratio" else 0)
    logs = 1 if argv[0] in ("backlash", "repeat") else 0
    assert held == ([(True, logs)] if hold else [])                # measurements saved first
    mount, = opened
    assert mount.drivers is not None and mount.drivers.configured
    assert not mount.enabled                                       # released on the way out
    if argv[0] == "backlash":
        assert mount.cfg.tilt.backlash_deg == 0.0                  # or it hides what it measures
