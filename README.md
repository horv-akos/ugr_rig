# UGR Evaluation Rig

A Raspberry Pi rig that measures discomfort glare in truck cabins. A Canon EOS 2000D on a motorised
pan-tilt head is aimed at defined directions of interest, captures HDR bracket sets, and the Pi
turns them into calibrated luminance maps and a CIE 117 Unified Glare Rating per direction. The
operator drives everything from a browser over the Pi's own WiFi.

The system exists to **compare cabins and configurations**, so repeatability between sessions
matters more than any single absolute number. Every design decision below follows from that.

---

## Hardware

| | |
|---|---|
| Controller | Raspberry Pi 4, 8 GB — headless, onboard WiFi as access point, 128 GB microSD (OS + raw archive) |
| Camera | Canon EOS 2000D + EF-S 18–55 mm taped at 18 mm (64 × 45°), USB tether, gphoto2/PTP for settings, shutter and CR2 download |
| Motion | 2-axis pan-tilt head, isaac879 CAD (MIT), 2× NEMA 17 (QSH4218-35) + TMC2209. Step/dir straight off Pi GPIO via pigpio; driver settings over UART |
| Zeroing | Optical — the camera images a fixed fiducial and the offset becomes the datum |
| Calibration | Certified lux meter + diffuse panel of known reflectance |
| Power | Dual-channel lab supply, always: CH1 12 V for motors and camera (USB-PD dummy battery), CH2 5.1 V for the Pi. Single ground tie, fused, motor kill switch. No vehicle power |

The Pi 4 is a hard requirement: `pigpio` drives GPIO through BCM2711 peripherals and does not
support the Pi 5.

---

## Measurement chain

1. **Aim** — head moves to a preset direction, settles, reports the commanded angle.
2. **Capture** — fixed CR2 bracket: f/8, ISO 100, 1/4000 s to 4 s in 2 EV steps (8 frames).
3. **Linearise** — raw → black level → 2×2 Bayer superpixels (3000 × 2000, no demosaic), channels
   mixed toward V(λ) with the camera's own colour matrix.
4. **Merge** — Debevec-weighted HDR merge on the linear data, then `L = k·DN·N²/(t·ISO)` → cd/m².
5. **Detect** — per-vehicle inclusion ROIs first, then an absolute 500 cd/m² threshold. Windows
   excluded by mask, not by rule.
6. **Background** — one coarse hemisphere sweep per session gives `E_ind`, and `L_b = E_ind/π`.
   Not the frame mean.
7. **Rate** — CIE 117 UGR, Guth position index via the Levin fit.

Every parameter that touched a result is stored with it, so any number can be recomputed.

---

## Software

```
ugr_rig/
├── hal/          mount: config · motion · backend · mount (built) · tmc2209 (UART, next)
│                 camera: camera_config · camera (gphoto2) · camera_sim (built)
├── config/       mount.toml · camera.toml · analysis.toml — every constant lives here
├── tools/        mountctl.py (head) · ugrctl.py (camera, calibration, processing, selftest)
├── photometry/   profile · linearise · hdr · calibration · masks (built)
├── analysis/     detection · background · ugr (built)
├── storage/      SQLite records, file layout, session archive
├── report/       heatmap (built) · PDF and CSV session reports
├── pipeline/     region (built) · job queue, session orchestration, optical zeroing, recovery
├── tests/        pytest: camera control flow against a fake libgphoto2, synthetic end to end
└── web/          FastAPI app + minimal frontend
```

`hal` is the only layer that touches hardware, so the whole photometry and analysis chain is
testable at a desk against stored CR2 sets, the camera has a simulator that renders a synthetic cab,
and the mount runs against a simulated pulse backend:

```
pip install -r requirements.txt
python -m tools.ugrctl --camera sim selftest
python -m tools.mountctl --backend sim selftest
```
Most iterations will happen there, not in a truck.

---

## Build order

| Step | Status |
|---|---|
| `hal/` mount: config, motion, pigpio/sim backends, `mountctl` | **Built**, sim-tested; not yet on hardware |
| `hal/tmc2209.py` — UART driver config | Next |
| `photometry` + `analysis` + heatmap, `pipeline/region.py`, `ugrctl` | **Built**, validated on synthetic brackets; needs real CR2s, the target and the meter |
| `hal/camera.py` — gphoto2 against the 2000D; this is where the surprises live | **Built**, tested against a fake libgphoto2; not yet on the body |
| Measure the built head (`docs/PIVOT_DETAIL.md` §8) | After assembly |
| `pipeline` + `storage` | Not started |
| `web` | Not started |
| `report` | Not started |

---

## Documentation

| Doc | What's in it |
|---|---|
| `REQUIREMENTS.md` | Measurement model, calibration, functional requirements, data model |
| `docs/PIVOT_OVERVIEW.md` | The pan-tilt head at a glance |
| `docs/PIVOT_DETAIL.md` | Full head spec: BOM, step generation, UART config, zeroing, API, measurements |
| `docs/CONNECTIONS.md` | Electrical wiring: rails, diagrams (+ rendered SVGs), pin tables, straps, bring-up |
| `hal/README.md` | Using and testing the mount driver |
| `docs/CAMERA_AND_PROCESSING.md` | Camera control, bracket, HDR merge, calibration, detection, UGR, outputs, `ugrctl` |

Purchasing material (costed BOM, shopping list, sourcing) and superseded documents are kept
outside this repository.

---

## Scope

**Not included:** camera slider, Bluetooth, game-controller input, battery monitoring, hardware
shutter trigger, hall-effect homing, keyframe/timelapse sequencing, any microcontroller, and any
vehicle power path — the rig always runs from a dual-channel lab supply.
Field-of-view coverage per direction is deferred to v2 — one frame per direction, sources outside
it are not counted.

## Open blockers

1. **Camera on the bench** — `ugrctl camera info` and one bracket on the real 2000D: which config
   names it exposes, which are writable, and whether the EXIF check passes.
2. **Tilt gear ratio** — 64:21 per the CAD, to be confirmed with the angle gauge before any
   commanded angle is trusted.
3. **Lab access to a calibrated luminance meter** — decides ~€550–1100 of the calibration budget.
4. **Motor mount clearance** — the 35 mm motor body is longer than the 23 mm the CAD assumes.
5. **Camera power** — the wiring and BOM still show the a7's NP-FW50 dummy battery. The 2000D
   needs an LP-E10 dummy fed from CH1, and an approved source for it.
6. **Lens constants** — focal length, distortion and vignetting of the 18–55 at 18 mm, f/8.
