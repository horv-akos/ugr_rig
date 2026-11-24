# `hal` — pan-tilt head driver and camera

The camera half (`camera_config.py`, `camera.py`, `camera_sim.py`) is documented in
`docs/CAMERA_AND_PROCESSING.md`. The rest of this file is the mount.

## Mount

Implements the interface in `docs/PIVOT_DETAIL.md` §6. Wiring: `docs/CONNECTIONS.md`.
Hardware-only: no camera,
no session logic. Optical zeroing belongs in `pipeline/`, which calls
`adjust_zero()` with the angle it worked out from the fiducial.

In the running system one process owns the mount: the rig server (`python -m web`,
`service/controller.py`), with `tools/rigctl.py` and the web pages as clients. The
head is open-loop, so a process that exits loses the position. `mountctl` opens
the mount directly and is for bench work and the measurement procedures only.

```
config.py    TOML -> dataclasses. Every physical constant lives in config/mount.toml
motion.py    trapezoidal profile -> merged two-axis timeline -> pigpio pulses
backend.py   PulseBackend: PigpioBackend (on the Pi) or SimBackend (on a desk)
mount.py     PanTiltMount: limits, backlash, step accounting, wave streaming
tmc2209.py   TMC2209 configuration over UART: DriverBus, SerialPort, SimTmcBus
```

## On the Pi

`pigpiod` must be running with `-t 1`, so it clocks off PCM rather than PWM, and the
UART must be enabled for the drivers. Units and the full setup: `deploy/README.md`.

## Driver configuration (`tmc2209.py`)

MS1/MS2 are UART address straps on this board, so an unconfigured driver runs PAN
1/8 and TILT 1/32 microstepping: every angle silently wrong. `enable()` therefore
first calls `DriverBus.configure()`: write GCONF (UART mode, microsteps from the
register), CHOPCONF (MRES from `microsteps`), IHOLD_IRUN (currents from
`[axis.*]`), clear GSTAT, then prove it: GCONF and CHOPCONF read back equal, and
the write counter IFCNT advanced once per write (IHOLD_IRUN is write-only). On
silence or a mismatch the drivers stay off.

Before every motion `check()` reads GSTAT and DRV_STATUS. A reset (motor rail
cycled) is reconfigured, the position marked uncertain and the move refused
(`DriverReset`). If configuring again fails, the motors are released (support the
tilt axis: it can drop), the zero is dropped and nothing moves until `enable()`
has verified both drivers (`DriverNotConfigured`). Enabling an already enabled
head checks first, so it cannot hide a reset. Over-temperature or a short refuses
the move too (`DriverFault`);
pre-warnings and open load show in `status()["drivers"]["warnings"]`.

Real hardware without a driver bus will not enable at all; the pulse simulator
runs with `SimTmcBus` (from the rig server and `mountctl`) or without one.

```
mountctl drivers                   # configure + read back, drivers stay off
mountctl --backend sim drivers     # the same against SimTmcBus
```

## Use

```python
from hal import PanTiltMount, install_signal_handlers

with PanTiltMount.from_config() as mount:
    install_signal_handlers(mount)
    mount.enable()
    mount.take_up_backlash()          # so the datum sits on the approach side
    mount.set_zero()                  # or adjust_zero() after optical zeroing
    result = mount.move_to(30.0, -8.0)  # blocks until settled
    print(mount.position())
```

`move_to()` raises rather than guessing:

| | |
|---|---|
| `NotZeroed` | no datum yet — power-up position is not 0 |
| `SoftLimit` | target outside the configured range; there are no limit switches |
| `MoveAborted` | `abort()` was called from another thread. Position becomes an estimate and `position_certain` goes False — re-zero before capturing |

`disable()` drops both the drive current and the datum, since the head can then
be turned by hand.

### Jog, nudge, speed, stop

| Call | Does |
|---|---|
| `jog(axis, ±1, speed, stop)` | Hold-to-move: runs until `stop` is set, then ramps down. With a datum it ends on the soft limit at worst; without one a single jog is capped at `unzeroed_jog_deg` (mount.toml), which is how the fiducial is found before zeroing |
| `nudge(pan_deg, tilt_deg, speed, stop)` | Relative step that always ends with the backlash taken up. With a datum it is `move_by`; without one it is capped like a jog |
| `take_up_backlash()` | For any axis that last moved against `approach_sign`: back off `backlash_deg`, return. Commanded position unchanged; the view shifts by the real backlash, so what is seen is what an absolute move reproduces |
| `speed=` on every move | Fraction of the configured top speed, 0 < speed ≤ 1. Acceleration is unchanged |
| `stop()` / a `stop` event | Controlled ramp-down. The step count stays exact, `position_certain` stays True, the call returns with `stopped=True` |
| `abort()` | Cuts the DMA stream at once. Position becomes an estimate |

Pass your own `threading.Event` as `stop`: it exists before the move starts, so a
stop sent in that gap is not lost. `stop()` and `abort()` are thread-safe.

The mount remembers per axis which way it last moved (`status()["backlash_ok"]`).
An absolute move to the angle the head already sits at is a no-op only when the
lash is on the approach side; after a jog the other way it backs off and comes in
again.

## How a move is built

1. Target degrees → steps, checked against the soft limits.
2. If an axis would arrive from the wrong side, the move is split: overshoot
   past the target by `backlash_deg`, then come back along `approach_sign`. Both
   stages are full two-axis moves, so DIR is static within each waveform.
3. Each axis gets a trapezoidal profile; the quicker one is stretched in time so
   both start and finish together. Stretching only lowers speed and accel.
4. The two step timelines merge into one, which becomes a single pulse stream —
   this is what lets both STEP pins run independent schedules in one wave.
5. The stream is cut into waves under the DMA pulse cap and at most `CHUNK_US`
   (100 ms) long, and sent back to back in sync mode. At most three waves exist
   at once regardless of move length, so a stop starts ramping within ~0.3 s. A
   full-travel move is about 700 waves and 69 s. At the head's few hundred
   microsteps/s a late wave costs a pause, not a lost step.
6. The timeline is consumed lazily. Once a stop is requested each axis ramps
   down from its current rate (measured from its last two steps) instead of
   finishing the plan.
7. After `wave_tx_busy()` clears, dwell `settle_ms` (skipped when stopped).

Position is kept in signed steps from zero, so nothing accumulates. Degrees are
a derived view.

## Off the rig

`SimBackend` counts every rising edge and records the DIR level in force at each
one, so the step maths, direction sense, backlash split and wave chunking are all
checkable at a desk:

```
python -m tools.mountctl --backend sim selftest
python -m tools.mountctl --backend sim info
```

`time_scale` controls simulated wall-clock: `0.0` runs instantly, `1.0` in real
time (which is what makes the abort and stop tests meaningful).

`pair(step_gpio, dir_gpio)` adds a signed step count per axis (`net_steps`): the
simulated head's physical position, which a new zero does not move. The rig server
uses it to point the simulated live view. `record_edges=False` drops the per-edge
log, which a long-running sim server would otherwise grow without bound.

## Measuring the head

`config/mount.toml` ships with placeholders marked `MEASURE`. `tools/mountctl.py`
runs the procedures from `docs/PIVOT_DETAIL.md` §8 and writes CSVs to
`measurements/`:

```
mountctl limits                        # §8.5 — do this first, drivers stay off
mountctl ratio --axis tilt --deg 30    # §8.1 — prints a gear_ratio line for mount.toml
mountctl backlash --axis pan --swing 5 # §8.3 — compensation off during the test
mountctl repeat --pan 30 --tilt 15 -n 10  # §8.4
```

Settle time (§8.2) needs the camera and is not in here.

## Known gaps

- **A driver reset on energising is unexplained.** `mountctl drivers` verifies both
  real drivers and both motors turn (2026-11-24), but one `goto` saw a reset right
  after ENABLE and a lost TILT write. Motor supply suspected, not confirmed.
- Aborting mid-move estimates the stopping point from elapsed time within the
  interrupted wave. It is flagged uncertain rather than trusted.
- `jog_steps()` bypasses limits and backlash by design. Bench use only.
- At a soft limit there may be no room to back off, so `take_up_backlash()` can
  leave an axis uncorrected (logged). The rig server then refuses to save a
  position or set the zero there.
