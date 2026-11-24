"""TMC2209 configuration over UART.

Why this exists: on this board MS1/MS2 are UART address straps, not microstep selectors, so a
driver nobody has configured runs PAN at 1/8 and TILT at 1/32 microstepping. Every angle would
be silently wrong (docs/CONNECTIONS.md §4.2). So before ENABLE is released, both drivers are
written and read back, and the mount refuses to enable on a mismatch or silence.

The UART carries settings only; motion stays on STEP/DIR from pigpio. One wire joins both
drivers' PDN_UART pins: the Pi's TX drives it through 1 kOhm and its RX sits on it, so every
byte the Pi sends comes straight back as an echo before any reply.

Datagrams (TMC2209 datasheet, UART interface):

    write   05 | addr | reg|0x80 | d31..d24 d23..d16 d15..d8 d7..d0 | crc      8 bytes
    read    05 | addr | reg                                         | crc      4 bytes
    reply   05 | ff   | reg      | 4 data bytes                     | crc      8 bytes

IHOLD_IRUN is write-only, so writes are verified with IFCNT, which counts every write the
driver accepted. GSTAT.reset says the driver lost its configuration (motor rail cycled); it is
checked before every move. DRV_STATUS reports over-temperature, shorts and open load.

`SerialPort` needs pyserial (BSD) and is imported only inside it, so this module loads on a
desk. `SimTmcBus` answers like two drivers on the wire, including the echo, strap defaults,
resets, a silent bus with the motor rail off, and faults on request.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import AxisConfig, DriversConfig
from .mount import MountError

log = logging.getLogger(__name__)

SYNC = 0x05
MASTER = 0xFF

# registers
GCONF, GSTAT, IFCNT, IOIN, IHOLD_IRUN, CHOPCONF, DRV_STATUS = 0x00, 0x01, 0x02, 0x06, 0x10, 0x6C, 0x6F

# GCONF bits
I_SCALE_ANALOG = 1 << 0
PDN_DISABLE = 1 << 6
MSTEP_REG_SELECT = 1 << 7
MULTISTEP_FILT = 1 << 8
# GSTAT bits
GSTAT_RESET, GSTAT_DRV_ERR, GSTAT_UV_CP = 1 << 0, 1 << 1, 1 << 2
# CHOPCONF fields
VSENSE = 1 << 17
MRES_SHIFT, MRES_MASK = 24, 0xF << 24
CHOPCONF_RESET = 0x10000053      # TOFF 3, HSTRT 5, intpol, MRES 0 (256)
# DRV_STATUS bits
OTPW, OT, S2GA, S2GB, S2VSA, S2VSB, OLA, OLB = (1 << i for i in range(8))

VERSION = 0x21                   # IOIN[31:24] on a TMC2209
VFS = {0: 0.325, 1: 0.180}       # full-scale sense voltage, vsense 0 / 1


class DriverError(MountError):
    """The drivers' configuration could not be set or verified. Drivers stay off."""


class DriverNoAnswer(DriverError):
    """Nothing on the UART: motor rail off, wiring, or the serial console still on the port."""


class DriverMismatch(DriverError):
    """A driver answered, but does not hold what was written."""


class DriverReset(DriverError):
    """A driver lost its configuration (motor rail cycled). It was configured again, but the
    motors were unpowered meanwhile, so the position is no longer certain."""


class DriverNotConfigured(DriverError):
    """A driver reset and could not be configured again, so its microstepping is unknown. The
    mount releases the motors; no move runs until `configure()` has verified both drivers."""


class DriverFault(DriverError):
    """Over-temperature shutdown or a short: the driver has switched its outputs off."""


# --------------------------------------------------------------------------- #
# framing
# --------------------------------------------------------------------------- #


def crc8(data: bytes) -> int:
    """CRC-8 of the TMC UART, polynomial x^8 + x^2 + x + 1, bits taken LSB first."""
    crc = 0
    for byte in data:
        for _ in range(8):
            if ((crc >> 7) ^ (byte & 1)) & 1:
                crc = ((crc << 1) ^ 0x07) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
            byte >>= 1
    return crc


def write_datagram(addr: int, reg: int, value: int) -> bytes:
    body = bytes([SYNC, addr, reg | 0x80]) + (value & 0xFFFFFFFF).to_bytes(4, "big")
    return body + bytes([crc8(body)])


def read_datagram(addr: int, reg: int) -> bytes:
    body = bytes([SYNC, addr, reg & 0x7F])
    return body + bytes([crc8(body)])


# --------------------------------------------------------------------------- #
# ports
# --------------------------------------------------------------------------- #


class UartPort(Protocol):
    def write(self, data: bytes) -> None: ...
    def read(self, n: int) -> bytes: ...      # up to n bytes; fewer on timeout
    def flush_input(self) -> None: ...
    def close(self) -> None: ...


class SerialPort:
    """The Pi's PL011 UART via pyserial (BSD)."""

    def __init__(self, device: str, baud: int, timeout_s: float) -> None:
        import serial  # pyserial; imported here so the module loads on a desk

        try:
            self._s = serial.Serial(device, baud, timeout=timeout_s)
        except serial.SerialException as exc:
            raise DriverNoAnswer(f"cannot open {device}: {exc} (enable_uart=1? serial console "
                                 "off? user in the dialout group?)") from exc

    def write(self, data: bytes) -> None:
        self._s.write(data)
        self._s.flush()

    def read(self, n: int) -> bytes:
        return self._s.read(n)

    def flush_input(self) -> None:
        self._s.reset_input_buffer()

    def close(self) -> None:
        self._s.close()


@dataclass
class SimDriver:
    straps_microsteps: int
    regs: dict[int, int] = field(default_factory=dict)
    ifcnt: int = 0

    def power_on(self) -> None:
        self.regs = {GCONF: I_SCALE_ANALOG, GSTAT: GSTAT_RESET, IOIN: VERSION << 24,
                     IHOLD_IRUN: 0x00011F10, CHOPCONF: CHOPCONF_RESET, DRV_STATUS: 0}
        self.ifcnt = 0

    @property
    def microsteps(self) -> int:
        """What the driver actually does: the register only once mstep_reg_select is set."""
        if self.regs[GCONF] & MSTEP_REG_SELECT:
            return 256 >> ((self.regs[CHOPCONF] & MRES_MASK) >> MRES_SHIFT)
        return self.straps_microsteps


class SimTmcBus:
    """Two TMC2209s on one wire, for desks and tests. Addresses and strap microstepping as
    wired: PAN 0 (1/8 unconfigured), TILT 1 (1/32 unconfigured)."""

    def __init__(self, straps: dict[int, int] | None = None) -> None:
        self.drivers = {a: SimDriver(m) for a, m in (straps or {0: 8, 1: 32}).items()}
        self.vmot = True              # motor rail: the drivers only talk with it on
        self.ignore_writes = False    # a driver that answers but keeps nothing
        self.corrupt_replies = 0      # how many replies to send with a bad CRC
        self._rx = bytearray()
        for d in self.drivers.values():
            d.power_on()

    def power_cycle(self) -> None:
        for d in self.drivers.values():
            d.power_on()

    def write(self, data: bytes) -> None:
        self._rx += data                         # the echo of a single-wire bus
        if not self.vmot or len(data) not in (4, 8) or data[0] != SYNC:
            return
        if crc8(data[:-1]) != data[-1]:
            return                               # the driver drops a bad datagram
        d = self.drivers.get(data[1])
        if d is None:
            return
        reg = data[2] & 0x7F
        if data[2] & 0x80:                        # write
            if self.ignore_writes:
                return
            value = int.from_bytes(data[3:7], "big")
            if reg == GSTAT:
                d.regs[GSTAT] &= ~value          # write 1 to clear
            else:
                d.regs[reg] = value
            d.ifcnt = (d.ifcnt + 1) & 0xFF
            return
        value = d.ifcnt if reg == IFCNT else d.regs.get(reg, 0)
        body = bytes([SYNC, MASTER, reg]) + value.to_bytes(4, "big")
        crc = crc8(body)
        if self.corrupt_replies:
            self.corrupt_replies -= 1
            crc ^= 0xFF
        self._rx += body + bytes([crc])

    def read(self, n: int) -> bytes:
        out, self._rx = bytes(self._rx[:n]), self._rx[n:]
        return out

    def flush_input(self) -> None:
        self._rx.clear()

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------- #
# one driver, the pair
# --------------------------------------------------------------------------- #


class Tmc2209:
    RETRIES = 2

    def __init__(self, port: UartPort, address: int, name: str) -> None:
        self.port, self.address, self.name = port, address, name

    def read(self, reg: int) -> int:
        req = read_datagram(self.address, reg)
        for _ in range(self.RETRIES + 1):
            self.port.flush_input()
            self.port.write(req)
            echo = self.port.read(len(req))
            if echo != req:
                raise DriverNoAnswer(f"{self.name}: UART echo missing or garbled "
                                     "(wiring: TX through 1 kOhm, RX on the bus)")
            reply = self.port.read(8)
            if len(reply) < 8:
                raise DriverNoAnswer(
                    f"{self.name} (address {self.address}) does not answer: motor rail on? "
                    "straps as wired?")
            if (reply[0] == SYNC and reply[1] == MASTER and reply[2] == reg
                    and crc8(reply[:7]) == reply[7]):
                return int.from_bytes(reply[3:7], "big")
            log.warning("%s: bad reply %s, retrying", self.name, reply.hex())
        raise DriverMismatch(f"{self.name}: replies keep failing their CRC (noise on the bus?)")

    def write(self, reg: int, value: int) -> None:
        dg = write_datagram(self.address, reg, value)
        self.port.flush_input()
        self.port.write(dg)
        if self.port.read(len(dg)) != dg:
            raise DriverNoAnswer(f"{self.name}: UART echo missing or garbled")


def current_scale(run_a: float, hold_a: float, rsense_ohm: float) -> tuple[int, int, int]:
    """(vsense, IRUN, IHOLD) for RMS currents: I = (CS + 1) / 32 * VFS / (Rsense + 20 mOhm)
    / sqrt 2. The low-voltage range (vsense 1) is used when the run current fits it: finer
    steps at the low currents a light head needs."""
    for vsense in (1, 0):
        k = 32 * math.sqrt(2) * (rsense_ohm + 0.02) / VFS[vsense]
        irun = round(run_a * k) - 1
        if irun <= 31:
            ihold = max(0, min(31, round(hold_a * k) - 1))
            return vsense, max(0, irun), ihold
    raise ValueError(f"run current {run_a} A is above what Rsense {rsense_ohm} ohm allows")


def amps(cs: int, vsense: int, rsense_ohm: float) -> float:
    return (cs + 1) / 32 * VFS[vsense] / (rsense_ohm + 0.02) / math.sqrt(2)


MRES_FOR = {256: 0, 128: 1, 64: 2, 32: 3, 16: 4, 8: 5, 4: 6, 2: 7, 1: 8}


class DriverBus:
    """Both drivers. `configure()` before ENABLE, `check()` before every move."""

    def __init__(self, port: UartPort, cfg: DriversConfig,
                 axes: tuple[AxisConfig, AxisConfig]) -> None:
        self.port = port
        self.cfg = cfg
        self.axes = axes
        self.drivers = {a.name: Tmc2209(port, a.uart_address, a.name) for a in axes}
        self.configured = False
        self.report: dict[str, Any] = {}

    def _targets(self, axis: AxisConfig) -> dict[int, int]:
        if axis.microsteps not in MRES_FOR:
            raise DriverMismatch(f"{axis.name}: {axis.microsteps} microsteps is not a TMC2209 setting")
        vsense, irun, ihold = current_scale(axis.run_current_a, axis.hold_current_a,
                                            self.cfg.rsense_ohm)
        chop = (CHOPCONF_RESET & ~MRES_MASK & ~VSENSE) | (MRES_FOR[axis.microsteps] << MRES_SHIFT)
        chop |= VSENSE if vsense else 0
        return {
            GCONF: PDN_DISABLE | MSTEP_REG_SELECT | MULTISTEP_FILT,   # UART, MRES from register
            CHOPCONF: chop,
            IHOLD_IRUN: (self.cfg.hold_delay << 16) | (irun << 8) | ihold,
        }

    def configure(self) -> dict[str, Any]:
        """Write both drivers, then prove it: GCONF and CHOPCONF read back equal, and IFCNT
        advanced once per write (IHOLD_IRUN cannot be read). Raises, never half-succeeds."""
        self.configured = False
        report: dict[str, Any] = {}
        for axis in self.axes:
            drv = self.drivers[axis.name]
            version = drv.read(IOIN) >> 24
            if version != VERSION:
                raise DriverMismatch(f"{axis.name}: IOIN version {version:#x}, expected a "
                                     f"TMC2209 ({VERSION:#x})")
            targets = self._targets(axis)
            n0 = drv.read(IFCNT)
            for reg, value in targets.items():
                drv.write(reg, value)
            drv.write(GSTAT, GSTAT_RESET | GSTAT_DRV_ERR | GSTAT_UV_CP)   # clear flags
            n1 = drv.read(IFCNT)
            expected = (n0 + len(targets) + 1) & 0xFF
            if n1 != expected:
                raise DriverMismatch(f"{axis.name}: driver accepted {(n1 - n0) & 0xFF} of "
                                     f"{len(targets) + 1} writes")
            for reg in (GCONF, CHOPCONF):
                got = drv.read(reg)
                if got != targets[reg]:
                    raise DriverMismatch(f"{axis.name}: register {reg:#04x} reads {got:#010x}, "
                                         f"wrote {targets[reg]:#010x}")
            if drv.read(GSTAT) & GSTAT_RESET:
                raise DriverMismatch(f"{axis.name}: reset flag will not clear")
            vsense = 1 if targets[CHOPCONF] & VSENSE else 0
            irun = (targets[IHOLD_IRUN] >> 8) & 0x1F
            ihold = targets[IHOLD_IRUN] & 0x1F
            report[axis.name] = {
                "address": axis.uart_address, "microsteps": axis.microsteps,
                "run_current_a": round(amps(irun, vsense, self.cfg.rsense_ohm), 3),
                "hold_current_a": round(amps(ihold, vsense, self.cfg.rsense_ohm), 3),
                "irun": irun, "ihold": ihold, "vsense": vsense,
                "gconf": f"{targets[GCONF]:#010x}", "chopconf": f"{targets[CHOPCONF]:#010x}",
            }
        self.configured = True
        self.report = report
        log.info("drivers configured: %s", report)
        return report

    def check(self) -> list[str]:
        """Before a move. A reset means the motor rail was cycled: reconfigure, then raise
        DriverReset so the caller marks the position uncertain. If that reconfigure fails,
        DriverNotConfigured, and again on every later check until `configure()` succeeds: the
        failed attempt may already have cleared the reset flag, so the flag alone cannot be
        trusted. Over-temperature shutdown and shorts raise DriverFault; pre-warnings and open
        load come back as warnings."""
        if not self.configured:
            raise DriverNotConfigured("the drivers are not configured and verified: "
                                      "enable them again")
        warnings: list[str] = []
        reset = []
        for axis in self.axes:
            drv = self.drivers[axis.name]
            gstat = drv.read(GSTAT)
            if gstat & GSTAT_RESET:
                reset.append(axis.name)
                continue
            st = drv.read(DRV_STATUS)
            if st & OT:
                raise DriverFault(f"{axis.name}: over-temperature shutdown")
            if st & (S2GA | S2GB | S2VSA | S2VSB):
                raise DriverFault(f"{axis.name}: short to ground or supply on a motor phase")
            if gstat & GSTAT_DRV_ERR:
                raise DriverFault(f"{axis.name}: driver error latched (short or overheat)")
            if st & OTPW:
                warnings.append(f"{axis.name}: driver over-temperature pre-warning")
            if st & (OLA | OLB):
                warnings.append(f"{axis.name}: open load (motor disconnected?)")
        if reset:
            try:
                self.configure()
            except DriverError as exc:
                raise DriverNotConfigured(
                    f"{' and '.join(reset)} driver reset (motor rail dip?) and configuring "
                    f"again failed ({exc}): motors released. Check the motor supply, enable "
                    "again and set the zero") from exc
            raise DriverReset(f"{' and '.join(reset)} driver reset (motor rail cycled?): "
                              "reconfigured, but the motors were unpowered, so the position "
                              "is uncertain. Set the zero again")
        return warnings

    def status(self) -> dict[str, Any]:
        return {"configured": self.configured, "port": self.cfg.port, "axes": self.report}

    def close(self) -> None:
        self.port.close()


def create_drivers(kind: str | None, cfg: DriversConfig,
                   axes: tuple[AxisConfig, AxisConfig]) -> DriverBus | None:
    """"uart" (the Pi), "sim" (SimTmcBus) or None."""
    if kind is None or kind == "none":
        return None
    if kind == "uart":
        return DriverBus(SerialPort(cfg.port, cfg.baud, cfg.timeout_s), cfg, axes)
    if kind == "sim":
        return DriverBus(SimTmcBus({a.uart_address: (8 if a.uart_address == 0 else 32)
                                    for a in axes}), cfg, axes)
    raise ValueError(f"unknown drivers {kind!r}: uart, sim or none")
