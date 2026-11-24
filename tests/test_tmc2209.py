"""TMC2209 configuration over UART, against the simulated bus, and its hold on the mount."""

from __future__ import annotations

from dataclasses import replace

import pytest

from hal import tmc2209 as t
from hal.backend import SimBackend
from hal.config import load_config
from hal.mount import MountError, PanTiltMount

CFG = replace(load_config(), settle_ms=0)


def bus() -> t.DriverBus:
    return t.create_drivers("sim", CFG.drivers, (CFG.pan, CFG.tilt))


def test_framing_matches_the_datasheet():
    assert t.read_datagram(0, t.GCONF).hex() == "05000048"
    dg = t.write_datagram(1, t.CHOPCONF, 0x14020053)
    assert dg[:7] == bytes([0x05, 0x01, 0xEC, 0x14, 0x02, 0x00, 0x53])
    assert t.crc8(dg[:7]) == dg[7]


@pytest.mark.parametrize("run,hold", [(0.3, 0.1), (0.6, 0.3), (1.0, 0.5), (1.5, 0.8)])
def test_currents_round_to_the_nearest_step(run, hold):
    vsense, irun, ihold = t.current_scale(run, hold, 0.11)
    step = t.amps(1, vsense, 0.11) - t.amps(0, vsense, 0.11)
    assert abs(t.amps(irun, vsense, 0.11) - run) <= step / 2 + 1e-9
    assert abs(t.amps(ihold, vsense, 0.11) - hold) <= step / 2 + 1e-9
    assert vsense == (1 if run <= t.amps(31, 1, 0.11) else 0)


def test_current_above_the_range_is_refused():
    with pytest.raises(ValueError):
        t.current_scale(3.0, 1.0, 0.11)


def test_configure_sets_microstepping_and_verifies():
    b = bus()
    sim = b.port
    assert [d.microsteps for d in sim.drivers.values()] == [8, 32]      # straps alone
    report = b.configure()
    assert [d.microsteps for d in sim.drivers.values()] == [16, 16]
    assert report["tilt"]["address"] == 1 and b.configured
    assert not sim.drivers[0].regs[t.GSTAT] & t.GSTAT_RESET              # flag cleared
    assert b.check() == []


@pytest.mark.parametrize("break_it,error", [
    (lambda s: setattr(s, "vmot", False), t.DriverNoAnswer),
    (lambda s: setattr(s, "ignore_writes", True), t.DriverMismatch),
    (lambda s: s.drivers[1].regs.__setitem__(t.IOIN, 0x20 << 24), t.DriverMismatch),
])
def test_configure_refuses_what_it_cannot_prove(break_it, error):
    b = bus()
    break_it(b.port)
    with pytest.raises(error):
        b.configure()
    assert not b.configured


def test_noise_is_retried_then_refused():
    b = bus()
    b.port.corrupt_replies = 1
    b.configure()
    b.port.corrupt_replies = 99
    with pytest.raises(t.DriverMismatch, match="CRC"):
        b.check()


def test_faults_and_warnings():
    b = bus()
    b.configure()
    b.port.drivers[0].regs[t.DRV_STATUS] = t.OTPW | t.OLA
    assert len(b.check()) == 2
    b.port.drivers[1].regs[t.DRV_STATUS] = t.OT
    with pytest.raises(t.DriverFault, match="over-temperature"):
        b.check()


def test_mount_configures_before_enable_and_checks_before_moves():
    b = bus()
    m = PanTiltMount(CFG, SimBackend(), b)
    b.port.vmot = False
    with pytest.raises(t.DriverNoAnswer):
        m.enable()
    assert not m.enabled
    b.port.vmot = True
    m.enable()
    assert m.status()["drivers"]["configured"]
    m.set_zero()
    m.move_to(1.0, 0.0)
    b.port.power_cycle()                     # motor rail cycled under a zeroed head
    with pytest.raises(t.DriverReset):
        m.move_to(0.0, 0.0)
    assert not m.position_certain
    m.move_to(0.0, 0.0)                      # reconfigured: moves again, still uncertain
    assert not m.position_certain
    m.close()


def lose_write(b: t.DriverBus, addr: int, reg: int) -> None:
    """The next write to that register is echoed but never accepted (noise, or another reset)."""
    sim = b.port
    real = sim.write

    def write(data: bytes) -> None:
        if len(data) == 8 and data[1] == addr and data[2] == reg | 0x80:
            sim.write = real
            sim._rx += data
            return
        real(data)

    sim.write = write


def test_a_reset_that_cannot_be_reconfigured_stops_the_head():
    """Seen on the bench 2026-11-24: a reset on energising, then "accepted 3 of 4 writes". The
    failed attempt still cleared the reset flag, so the next move ran at 1/256."""
    b, sim = bus(), SimBackend()
    m = PanTiltMount(CFG, sim, b)
    m.enable()
    m.set_zero()
    b.port.power_cycle()
    lose_write(b, 1, t.CHOPCONF)
    with pytest.raises(t.DriverNotConfigured, match="pan and tilt driver reset.*3 of 4 writes"):
        m.move_to(0.0, 5.0)
    assert not b.port.drivers[1].regs[t.GSTAT] & t.GSTAT_RESET   # why the flag is not enough
    assert not m.enabled and not m.is_zeroed and not m.position_certain
    assert sim.levels[CFG.pins.enable] == 1                      # motors off
    edges = dict(sim.rising_edges)
    with pytest.raises(MountError):
        m.move_to(0.0, 5.0)
    with pytest.raises(MountError, match="not enabled"):
        m.jog_steps(10, 0)
    with pytest.raises(t.DriverNotConfigured):
        b.check()
    assert sim.rising_edges == edges                             # nothing stepped
    m.enable()                                                   # verified again
    assert [d.microsteps for d in b.port.drivers.values()] == [16, 16]
    assert not m.position_certain
    m.set_zero()
    m.move_to(0.0, 1.0)
    assert m.position_certain
    m.close()


def test_enabling_again_does_not_hide_a_reset():
    b = bus()
    m = PanTiltMount(CFG, SimBackend(), b)
    m.enable()
    m.set_zero()
    b.port.power_cycle()
    with pytest.raises(t.DriverReset):
        m.enable()
    assert not m.position_certain
    m.enable()
    assert m.enabled and not m.position_certain
    m.close()


def test_enabling_again_releases_when_it_cannot_verify():
    b, sim = bus(), SimBackend()
    m = PanTiltMount(CFG, sim, b)
    m.enable()
    m.set_zero()
    b.port.ignore_writes = True
    with pytest.raises(t.DriverMismatch):
        m.enable()
    assert not m.enabled and not m.position_certain
    assert sim.levels[CFG.pins.enable] == 1
    m.close()


def test_real_hardware_without_a_driver_bus_will_not_enable():
    """Anything that is not the pulse simulator counts as real hardware."""
    sim = SimBackend()
    pigpio_like = type("PigpioLike", (), {k: getattr(sim, k) for k in (
        "set_output", "write", "wave_clear", "wave_get_max_pulses", "wave_tx_stop", "close")})()
    m = PanTiltMount(CFG, pigpio_like)
    with pytest.raises(MountError, match="no driver UART"):
        m.enable()
    assert not m.enabled
