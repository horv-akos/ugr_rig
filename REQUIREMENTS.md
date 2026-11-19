# UGR Evaluation Rig — Requirements & Architecture

Status: draft v0.5 · 2026-10-13 (camera changed to Canon EOS 2000D; capture + processing built)
Repository: `ugr_rig` (self-contained; `ugr_analyzer` is a superseded proof-of-concept,
mined for reference only)

Scope, hardware summary and build order live in `README.md`. The pan-tilt head has its own docs:
`docs/PIVOT_OVERVIEW.md` and `docs/PIVOT_DETAIL.md`; wiring in `docs/CONNECTIONS.md`. This document covers the measurement model,
calibration and the functional requirements.

---

## 1. Purpose

A Raspberry Pi controlled rig that measures discomfort glare in truck cabins. A Canon EOS 2000D on a
pan/tilt head is aimed at defined directions of interest, captures HDR bracket sets, and the
Pi converts them into calibrated luminance maps and a CIE 117 Unified Glare Rating per direction.
The operator drives everything from a browser over the Pi's own WiFi.

The system exists to **compare cabin configurations and vehicles**, so repeatability between
sessions matters more than any single absolute number.

---

## 2. Hardware baseline

| Item | Choice | Notes |
|---|---|---|
| Controller | Raspberry Pi 4, 8 GB | Headless, onboard WiFi as AP, 128 GB microSD. Pi 4 specifically — pigpio has no Pi 5 support |
| Camera | Canon EOS 2000D, EF-S 18–55 mm taped at 18 mm | USB tether, PTP via libgphoto2 (python-gphoto2, LGPL-3.0). CR2 raw. APS-C, 6000 × 4000, 64 × 45° at 18 mm |
| Motion | 2-axis pan/tilt head, isaac879 CAD (MIT) | 2× NEMA 17 (QSH4218-35) + TMC2209, driven directly from Pi GPIO |
| Motion interface | Step/dir: pigpio DMA waveforms, 5 GPIO. Driver config: UART, 2 GPIO | No microcontroller. UART carries settings only |
| Zeroing | Optical, camera + fixed fiducial | ~0.05°; doubles as the end-of-session drift check |
| Calibration reference | Certified lux meter + diffuse panel of known ρ | Operator-in-the-loop, once per session |
| Power | Dual-channel lab supply, always: CH1 12 V (motors, camera via USB-PD), CH2 5.1 V (Pi) | Single ground tie. No vehicle power path |

Angular resolution is 0.0133° pan and 0.0369° tilt at 1/16 microstepping — far beyond
requirement. Steppers hold position statically, so there is no dither during long bracket
exposures. Full head spec in `docs/PIVOT_DETAIL.md`.

### Known hardware risks

- **Payload.** The design is tested to just over 1 kg (Canon 250D + 18–135). The 2000D body is
  ~475 g with battery and the EF-S 18–55 ~200 g, so ~0.7 kg — inside the budget. The lens is
  taped at 18 mm: focal length enters every solid angle, so it must not drift between sessions.
- **Open-loop.** A stall from an over-budget payload or aggressive acceleration corrupts position
  silently. Mitigation: conservative speed and acceleration, and the end-of-session optical drift
  check — a residual above 0.1° marks that session's angles suspect.
- **Backlash** in the printed gears is the dominant angular error, not step size. Always approach
  a target angle from the same direction.
- **Vibration.** Axis motion excites the arm. Capture must not begin until the head has settled;
  settle time is measured on the built head, not assumed.
- **Tilt gear ratio.** 64:21, confirmed against the angle gauge. The upstream 123:16 belt constant
  would scale every tilt angle by 2.52×, so don't copy constants from upstream firmware.

---

## 3. Measurement model

### 3.1 What counts as a glare source

Interior luminaires and displays only: dome and reading lights, instrument cluster and dash
backlighting, infotainment screens, switch backlighting. Daylight through the windshield and
side glass is **excluded** — UGR is not validated for large area daylight sources, and including
them would produce numbers that look authoritative but are not defensible.

Windows are excluded by explicit per-vehicle exclusion masks, not by a luminance rule.

### 3.2 Detection

Absolute luminance threshold, default **500 cd/m²**, configurable per vehicle profile and
recorded with every result. The relative threshold used in the proof-of-concept (5× the 80th
percentile) is rejected: it drifts with the scene, so a dash reflection becomes a "luminaire" at
night while the cluster drops out in daylight. That destroys cross-session comparability.

In addition, per-vehicle **inclusion ROIs** may be defined for each known luminaire. Where an ROI
exists it takes precedence over blob detection. Operators know where the dome light is; encoding
that once per truck is more reliable than any detector.

### 3.3 Background luminance

L_b is defined per CIE 117 as `E_ind / π`, where `E_ind` is the indirect vertical illuminance at
the eye — the whole hemisphere excluding the glare sources.

It is **not** the mean of the non-source pixels in a single frame. A 64 × 45° frame (0.81 sr) sees
about an eighth of the hemisphere, so the frame mean depends on where the camera happens to point. Since
UGR shifts by `8·log₁₀` of the L_b error, a factor-2 error costs 2.4 UGR points — more than a
whole perception category.

**Requirement:** each session performs one coarse hemisphere sweep (low resolution, short
bracket) from which `E_ind` is integrated. The resulting L_b is used for every direction in that
session and stored in the session record.

Optional cross-check, recommended: a cosine-corrected illuminance sensor at the eye point,
logged alongside the computed value.

### 3.4 Position index

Guth position index via the **Levin (1975) analytical fit**, the same form used by Evalglare and
the IES, with Iwata's extension for sources below the line of sight.

The proof-of-concept used `p = 1 + 0.8·(φ/10)^1.2` with `φ = √(H² + (3T)²)`. That curve has no
published basis and is symmetric in the vertical direction, whereas Guth + Iwata is asymmetric —
the brow shields overhead sources, so a source 30° above the line of sight gets p ≈ 3.3 while the
same source 30° below gets p ≈ 1.5: sources below the line of sight count roughly 5× more. Since contribution goes
as `1/p²`, a 30% error in p is 1.8 UGR points per source, and the error grows with eccentricity,
which is exactly where cabin luminaires sit.

### 3.5 UGR

Standard CIE 117:1995 form, unchanged:

```
UGR = 8 · log₁₀ [ (0.25 / L_b) · Σᵢ ( Lᵢ² · ωᵢ / pᵢ² ) ]
```

Every parameter that influenced the result (threshold, L_b, k, lens, masks version) is stored
with the result so any number can be reproduced or recomputed.

### 3.6 Deferred

Field-of-view coverage is **explicitly out of scope for v1**. One frame per direction; sources
outside the frame are not counted. This is a known under-reporting bias, to be revisited (mosaic
capture or a wider lens) once the rest of the chain works.

---

## 4. Photometric calibration

### 4.1 Model

Luminance is recovered from the linearised raw value with an exposure-invariant model:

```
L = k · DN · N² / (t · ISO)
```

- `DN` — dark-subtracted, linear raw value from the CR2, weighted across channels toward V(λ)
  (the Y row of the camera's own colour matrix, read from the file)
- `N`, `t`, `ISO` — read from EXIF, per frame
- `k` — calibration constant for a given camera + lens + aperture

Exposure invariance is not optional: HDR bracketing changes `t` between frames, so putting frames
on a common scale *is* the merge.

### 4.2 Session calibration

Per session, the operator shoots the matt target and enters the handheld meter reading. The
measured `k` **validates** the stored lens value rather than replacing it. Drift beyond ±10%
raises a warning in the UI — that is the catch for a swapped lens, a dirty filter, a changed
aperture, or in-camera processing left enabled.

Useful context on tolerance: a pure scale error in `k` largely cancels, because UGR carries `L²`
in the numerator and `L_b` in the denominator. Scaling every luminance by `c` shifts UGR by only
`8·log₁₀(c)` — 10% off in `k` is 0.33 UGR points.

### 4.3 What does not cancel

Spectral error. A Bayer sensor's response is not V(λ). Calibrating `k` on a neutral target and
then measuring an amber cluster backlight or a coloured display can be 10–30% off *for that
source relative to the background*, and that error enters UGR undamped.

**Requirement:** support a table of per-source-type spectral correction factors (white LED,
amber, red, display) measured against the handheld meter, selectable per ROI. v1 may ship with
the table empty and a single global factor.

### 4.4 Capture constraints

- CR2 only, no JPEG
- Mode dial on M, lens switch on MF — both physical on the 2000D; the code checks, it cannot set
- Highlight Tone Priority off (it changes the raw exposure and the ISO floor). Long-exposure noise
  reduction off (harmless to the data, doubles the time of the 1 s and 4 s frames)
- Fixed white balance (metadata only — does not touch raw)
- Vignetting map per lens and aperture, measured once
- Shutter labels are rounded: "1/60" is timed as 1/64 s. The merge uses the APEX value
  (`shutter_model = "apex"`); using the labels leaves 3–7 % seams between frames. Every merge
  reports the measured frame-to-frame ratio, which is the check that the timing assumption holds

---

## 5. Functional requirements

### 5.1 Motion

The Pi generates step/dir directly through pigpio; all motion logic is Python. See
`docs/PIVOT_DETAIL.md` for the implementation.

- Command pan and tilt in degrees, absolute; verify the gear-ratio constants empirically before
  trusting any commanded angle
- Optical zero at session start; repeat at session end and log the drift
- Refuse to move before a zero has been set — power-up position is unknown
- Always approach a target from a consistent direction to absorb gear backlash
- Both axes move together, in one waveform
- Configurable settle delay after motion completes; no capture while moving
- Soft limits per axis, checked before motion is generated — there are no limit switches
- Report actual commanded angle with every capture
- Release the driver enable line and stop pulse transmission on any exit path
- Configure both drivers over UART (microstepping, run/hold current) and read the settings back
  before enabling; refuse to enable on mismatch or no answer
- Detect a driver reset (motor rail cycled) before each move; reconfigure and mark position
  uncertain

### 5.2 Capture

- Detect and connect the 2000D over USB; surface a clear state when absent, busy or in the wrong mode
- Set image format, capture target, aperture and ISO over PTP; set shutter per frame
- **Fixed bracket**, decided 2026-10-13: f/8, ISO 100, 1/4000 s to 4 s in 2 EV steps (8 frames),
  shortest first. Covers ~0.5 to ~200 000 cd/m². Configurable in `config/camera.toml`
- Report pixels still clipped in the shortest frame; their luminance is a lower bound and every
  source containing them is flagged (no automatic extension with a fixed bracket)
- Download CR2s to the Pi and verify (size against the camera, EXIF shutter/aperture/ISO against
  the request, LibRaw decode) before deleting from the card
- Fail loudly and recoverably: a dropped USB link mid-session must not lose completed directions

### 5.3 Processing

- Linearise CR2 (rawpy), subtract black level per channel, apply vignetting map
- **Downscale early**: each 2×2 Bayer quad becomes one pixel (3000 × 2000), no demosaic. UGR does
  not need full 24 MP resolution, and the Pi 4 does not have it to spare
- HDR merge: Debevec & Malik weighting (hat function on the brightest channel of each quad) on
  exposure-normalised linear data. The response-curve half of Debevec is not needed — raw is
  already linear
- Apply `k` and spectral factors → luminance map in cd/m²
- Build ω and Guth masks from lens/FOV; cache per lens + resolution
- Detect sources (ROIs first, then threshold), compute per-source L, ω, p
- Compute UGR against session L_b

Target: under 60 s per direction end to end on a Pi 4.

### 5.4 Web interface

Runs headless on the Pi's own access point; used from a phone or laptop in the cab.

- Vehicle profiles: create, edit, select
- Preset direction lists per vehicle — the primary way directions are chosen, so the same set
  is re-run identically on every visit
- Session workflow: select vehicle → optical zero → calibration shot → hemisphere sweep → run
  preset list → drift check → review
- Live status: current direction, capture progress, processing queue, errors
- Per-direction result view: UGR, L_b, source table, false-colour luminance map
- Session review and export
- Comparison view: same direction across vehicles or configurations
- Shut down the Pi cleanly from the UI — the OS and archive are on a microSD card, which corrupts
  if the supply is switched off under it

Long operations run as background jobs with progress reported to the UI; the browser must be
able to disconnect and reconnect without losing a session.

### 5.5 Data and export

Every session retains:

- **Raw archive** — original CR2s, so any measurement can be recomputed later with different
  settings
- **CSV** — flat results table for Excel/Python
- **PDF report** — per-direction UGR, luminance map, source table, plus the session calibration
  record, the zero drift figure and all parameters
- Full parameter provenance on every result

---

## 6. Data model

```
Vehicle          id, name, notes, exclusion_masks, inclusion_rois, threshold_override
Preset           vehicle_id, name, pan_deg, tilt_deg, order
Session          id, vehicle_id, started_at, operator, lens, aperture,
                 k_stored, k_measured, k_drift_flag, L_b, E_ind,
                 zero_ref, zero_drift_deg, ambient_notes
Measurement      session_id, preset_id, pan_deg, tilt_deg, bracket_file_ids,
                 luminance_map_ref, ugr, n_sources, params_snapshot
Source           measurement_id, roi_name, L_cd_m2, omega_sr, p, contribution
CalibrationRec   lens, aperture, k, vignette_map_ref, spectral_factors, measured_at
```

SQLite for records, files on disk for images and maps.

---

## 7. Module layout

```
ugr_rig/
├── hal/
│   ├── config.py         config/mount.toml → dataclasses                    (built)
│   ├── motion.py         trapezoidal profiles, merged two-axis pulse stream  (built)
│   ├── backend.py        pigpio backend + desk simulator                     (built)
│   ├── mount.py          limits, backlash, step accounting, wave streaming   (built)
│   ├── tmc2209.py        UART register layer: config, read-back, status      (next)
│   ├── camera_config.py  config/camera.toml, shutter arithmetic              (built)
│   ├── camera.py         gphoto2/PTP: settings, bracket capture, download    (built, mock-tested)
│   └── camera_sim.py     synthetic cab scene → Bayer frames                  (built)
├── photometry/
│   ├── profile.py        config/analysis.toml → dataclasses                  (built)
│   ├── linearise.py      CR2 → Bayer superpixels (rawpy), black level        (built)
│   ├── hdr.py            Debevec-weighted merge on linear data               (built)
│   ├── calibration.py    k model, target measurement, vignetting             (built)
│   └── masks.py          per-pixel direction + solid angle, Guth/Iwata p     (built)
├── analysis/
│   ├── detection.py      ROI + absolute-threshold source extraction          (built)
│   ├── background.py     frame mean (provisional) + hemisphere sweep → L_b   (built)
│   └── ugr.py            CIE 117 formula, perception scale                   (built)
├── storage/              SQLite models, file layout, session archive
├── report/               heatmap (built); PDF and CSV session reports
├── pipeline/             region.py: one direction end to end (built); job queue, session
│                         orchestration, optical zeroing, recovery
└── web/                  FastAPI app + minimal frontend
```

Rationale: `hal` is the only layer that touches hardware, so the whole photometry and analysis
chain is testable off-rig against stored CR2 sets. That matters — most iterations will happen at
a desk, not in a truck.

Optical zeroing sits in `pipeline/` because it needs the mount and the camera together; `hal`
stays testable without either.

---

## 8. Suggested build order

Status per step: `README.md`.

1. `photometry` + `analysis` offline, validated against a known target and the handheld meter
2. `hal/camera.py` — verify gphoto2 behaviour with the 2000D early (`ugrctl camera info`, then one
   bracket); this is the most likely source of unpleasant surprises
3. `hal/` mount (built, sim-tested) + `hal/tmc2209.py`, on the bench with a logic analyser, then
   on the built head: gear ratio, settle time, backlash, repeatability, run/hold current
4. `pipeline` + `storage`, including optical zeroing and the drift check
5. `web`
6. `report`

---

## 9. Open items

- Field-of-view coverage per direction (deferred, section 3.6)
- Lens constants for the EF-S 18–55 at 18 mm: effective focal length, radial distortion k1,
  vignetting map at f/8 — all `MEASURE` in `config/analysis.toml`
- Raw clip level of the 2000D at ISO 100 (`saturation_fraction`, currently a conservative 0.92)
- Vibration and settle time — needs measuring on the actual head
- Whether the illuminance sensor is added for L_b cross-check
- Spectral correction factor table — which source types matter in practice
- 2000D over gphoto2: libgphoto2 2.5.34 lists it with capture, config, trigger and preview. Still
  to confirm on the body: config names, that `capturetarget`, `iso`, `aperture` are writable, and
  how `focusmode` reports the lens switch
- Line of sight: each region is rated as if the observer looks along the camera axis
  (`line_of_sight = "camera"`). A fixed forward gaze is supported but not the default
- Small sources (ω < 0.0003 sr, e.g. single indicator LEDs) are outside CIE 117 and flagged; CIE
  146/147 small-source correction needs the distance to the source, which the camera does not have
- Fiducial target design and where it is mounted in a cab (§5.1 zeroing)
- Hold current: how low it can go before the tilt axis creeps during a bracket
- Approved-supplier source for the USB-PD module (currently Amazon)
- Pi 4 processing time per direction: ~8 s on a desktop at full resolution, so an estimated
  30–45 s on the Pi 4 — measure against the 60 s target
