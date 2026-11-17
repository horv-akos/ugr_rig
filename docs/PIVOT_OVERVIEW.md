# Pan-Tilt Head — overview

Two-axis motorised head carrying the a7. Mechanics are isaac879's 3D-printed Pan-Tilt-Mount
(MIT); everything electrical and every line of control code is ours. Full detail in
`PIVOT_DETAIL.md`, wiring in `CONNECTIONS.md`.

## What it does

Aims the camera at an absolute (pan, tilt) angle, holds still while the bracket is exposed, and
reports where it actually pointed. That is the entire job.

## Key numbers

| | Pan | Tilt |
|---|---|---|
| Gear ratio | 144:17 = 8.4706 | 64:21 = 3.0476 |
| Steps/degree at 1/16 | 75.29 | 27.09 |
| Resolution | 0.0133° | 0.0369° |
| Working speed | ~5 °/s, deliberately slow | |
| Payload | tested to just over 1 kg | |
| Dominant error | printed-gear backlash, not step size | |

## How it's controlled

The Raspberry Pi generates step and direction pulses directly, through pigpio's DMA-timed
waveforms — no microcontroller, no firmware. Five GPIO lines for motion: two step, two
direction, one shared enable. Both axes ride in the same waveform, so they move together rather
than one after the other.

The two TMC2209 drivers are configured over a shared UART line (two more GPIO): microstepping,
run and hold current are set in software and read back before the drivers are enabled. UART
carries settings only, never motion.

Because the Pi queues the pulses itself, it knows exactly when motion ends. Settle time is a
measured dwell after that, not a guess.

## Zeroing

No home switch. The camera is the position reference:

1. Level the base — that fixes tilt's gravity datum.
2. Put a small bright fiducial at the reference direction; leave it untouched for the session.
3. Capture, centroid it, convert the pixel offset to an angle via the FOV calibration, apply as
   the datum. Good to ~0.05°.
4. Repeat at end of session. Residual above 0.1° means steps were lost — flag every angle in
   that session.

Pan zero references the cab centreline, tilt zero references horizontal, so both mean the same
thing in every truck.

## Cost

Roughly €130 of motion electronics (motors, drivers, perfboard, connectors) plus ~€150 of
mechanics (printed parts, bearings, M3 hardware). Costed lines in `SHOPPING_LIST.md` §2–§3 (purchasing doc, kept outside this repo).

## Not included

No slider axis, no hall sensors or magnets, no Arduino, no custom PCB, no Bluetooth, no game
controller, no battery monitoring, no hardware shutter trigger, no limit switches, no StallGuard,
no vehicle power — always a dual-channel lab supply. Soft limits in
the config file are the only thing preventing a bug from driving the camera into the frame —
measure the interference points by hand and enter them before the first automated move.
