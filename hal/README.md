# `hal` — pan-tilt head driver and camera

The camera half (`camera_config.py`, `camera.py`, `camera_sim.py`) is documented in
`docs/CAMERA_AND_PROCESSING.md`. The rest of this file is the mount.

## Mount

Implements the interface in `docs/PIVOT_DETAIL.md` §6. Wiring: `docs/CONNECTIONS.md`.
Hardware-only: no camera,
no session logic. Optical zeroing belongs in `pipeline/`, which calls
`adjust_zero()` with the angle it worked out from the fiducial.

```
config.py    TOML -> dataclasses. Every physical constant lives in config/mount.toml
motion.py    trapezoidal profile -> merged two-axis timeline -> pigpio pulses
backend.py   PulseBackend: PigpioBackend (on the Pi) or SimBackend (on a desk)
mount.py     PanTiltMount: limits, backlash, step accounting, wave streaming
```

## On the Pi

`pigpiod` must be running, and with `-t 1` so it clocks off PCM rather than PWM:

```ini
# /etc/systemd/system/pigpiod.service
[Service]
ExecStart=/usr/bin/pigpiod -t 1 -l    # -l = local connections only
Type=forking
```

The app service takes `Requires=pigpiod.service` and `After=pigpiod.service`.

## Use

```python
from hal import PanTiltMount, install_signal_handlers

with PanTiltMount.from_config() as mount:
    install_signal_handlers(mount)
    mount.enable()
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

`abort()` is thread-safe and is what the web layer's cancel button calls.
`disable()` drops both the drive current and the datum, since the head can then
be turned by hand.

## How a move is built

1. Target degrees → steps, checked against the soft limits.
2. If an axis would arrive from the wrong side, the move is split: overshoot
   past the target by `backlash_deg`, then come back along `approach_sign`. Both
   stages are full two-axis moves, so DIR is static within each waveform.
3. Each axis gets a trapezoidal profile; the quicker one is stretched in time so
   both start and finish together. Stretching only lowers speed and accel.
4. The two step timelines merge into one, which becomes a single pulse stream —
   this is what lets both STEP pins run independent schedules in one wave.
5. The stream is cut into waves under the DMA pulse cap and sent back to back in
   sync mode, so at most three waves exist at once regardless of move length. A
   full-travel move is about five waves and 69 s.
6. After `wave_tx_busy()` clears, dwell `settle_ms`.

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
time (which is what makes the abort test meaningful).

## Measuring the head

`config/mount.toml` ships with placeholders marked `MEASURE`. `tools/mountctl.py`
runs the procedures from `docs/PIVOT_DETAIL.md` §8 and writes CSVs to
`measurements/`:

```
mountctl limits                        # §8.5 — do this first, drivers stay off
mountctl ratio --axis tilt --deg 30    # §8.1 — 64:21 vs 123:16 is a 2.52x error
mountctl backlash --axis pan --swing 5 # §8.3 — compensation off during the test
mountctl repeat --pan 30 --tilt 15 -n 10  # §8.4
```

Settle time (§8.2) needs the camera and is not in here.

## Known gaps

- **UART driver configuration is not written yet** (`hal/tmc2209.py`, next). The
  hardware is wired for UART: MS1/MS2 are address straps, so until the drivers
  are configured PAN runs at 1/8 and TILT at 1/32 microstepping, not the 1/16
  this code assumes. Do not run moves on the real head before `tmc2209.py`
  exists and verifies the configuration. Spec: `docs/PIVOT_DETAIL.md` §4.4,
  `docs/CONNECTIONS.md` §6.
- Aborting mid-move estimates the stopping point from elapsed time within the
  interrupted wave. It is flagged uncertain rather than trusted.
- `jog_steps()` bypasses limits and backlash by design. Bench use only.
