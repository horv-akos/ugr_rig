# UGR Evaluation Rig — Requirements & Architecture

Status: draft v0.6 · 2026-11-19 (operator workflow and the three web pages, live view, motor
control service, windscreen reflections, forward-gaze study rating, report images, metrics and
Excel export). v0.5 · 2026-10-13: camera changed to Canon EOS 2000D; capture + processing built
Repository: `ugr_rig` (self-contained; `ugr_analyzer` is a superseded proof-of-concept,
mined for reference only)

Scope, hardware summary and build order live in `README.md`. The pan-tilt head has its own docs:
`docs/PIVOT_OVERVIEW.md` and `docs/PIVOT_DETAIL.md`; wiring in `docs/CONNECTIONS.md`. This document covers the measurement model,
calibration and the functional requirements.

---

## 1. Purpose

A Raspberry Pi controlled rig that measures discomfort glare in truck cabins. A Canon EOS 2000D on a
pan/tilt head is aimed at defined directions of interest, captures HDR bracket sets, and the
Pi converts them into calibrated luminance maps, a CIE 117 Unified Glare Rating per direction and
one forward-gaze rating for the whole study (§3.6). Reflections of the cab's own lights in the
glazing count as glare sources (§3.1). The operator drives everything from a browser over the Pi's
own WiFi.

The system exists to **compare cabin configurations and vehicles**, so repeatability between
sessions matters more than any single absolute number.

---

## 2. Hardware baseline

| Item | Choice | Notes |
|---|---|---|
| Controller | Raspberry Pi 4, 8 GB | Headless, onboard WiFi as AP, 128 GB microSD. Pi 4 specifically — pigpio has no Pi 5 support |
| Camera | Canon EOS 2000D, EF-S 18–55 mm taped at 18 mm | USB tether, PTP via libgphoto2 (python-gphoto2, LGPL-3.0). CR2 raw; live view over the same link. APS-C, 6000 × 4000, 64 × 45° at 18 mm |
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

Interior luminaires and displays, seen directly **or reflected in the glazing**: dome and reading
lights, instrument cluster and dash backlighting, infotainment screens, switch backlighting.
Reflections of these in the windscreen and side glass are a primary concern of the study (decided
2026-11-19): at night the driver looks through the windscreen, so a reflected display sits close
to the line of sight and counts heavily.

Daylight through the glazing stays **excluded**. UGR is not validated for large area daylight
sources, and including them would produce numbers that look authoritative but are not defensible.

So glazing is no longer simply masked out. Each position's masks mark it as a **glazing** region,
and what happens there depends on the study's lighting condition:

| Lighting condition | In glazing regions |
|---|---|
| `dark`: night, or a darkened hall | Sources detected against the reflection threshold (§3.2), tagged `reflection`, counted in UGR |
| `daylight` | Excluded, as before. Reflections cannot be told apart from the bright scene behind the glass |

Glazing pixels that are not sources count toward the background in both conditions
(`windows_in_background`). Exclusion polygons remain for anything that must never count, such as a
known light outside the vehicle. The lighting condition is set per study and recorded with every
result.

Built so far (2026-11-19): glazing polygons are drawn per position in the mask editor and read by
detection, which treats them as excluded (the `daylight` rule) until reflection detection exists.
Every result with glazing carries a flag saying reflections in it are not counted yet.

### 3.2 Detection

Absolute luminance threshold, default **500 cd/m²**, configurable per position profile and
recorded with every result. The relative threshold used in the proof-of-concept (5× the 80th
percentile) is rejected: it drifts with the scene, so a dash reflection becomes a "luminaire" at
night while the cluster drops out in daylight. That destroys cross-session comparability.

In addition, per-position **inclusion ROIs** may be defined for each known luminaire. Where an ROI
exists it takes precedence over blob detection. Operators know where the dome light is; encoding
that once per truck is more reliable than any detector.

Order: exclusion polygons → inclusion ROIs → glazing (reflections) → everything else (threshold).

**Reflections** need their own parameters:

- A separate, lower **reflection threshold**. Glass reflects a few percent at normal incidence and
  considerably more at the grazing angles of a raked windscreen, so the reflection of an
  8 000 cd/m² dome light lands near the 500 cd/m² direct threshold. Absolute, configurable per
  position profile, recorded with every result. Default: open item (§9).
- A separate **merge radius**. Laminated glass gives a double image, one per surface, which can sit
  further apart than `merge_radius_px`; the pair must count as one reflection.
- An optional `reflects` link from a reflection ROI to the luminaire ROI it mirrors. The report can
  then split a luminaire's glare into direct and reflected, and the reflection inherits the
  parent's source type for the spectral factor (glass reflection is close to neutral).

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

### 3.6 Per-position and forward-gaze rating

Two ratings, decided 2026-11-19 (this replaces the v0.5 deferral of field-of-view coverage):

- **Per position** (`line_of_sight = "camera"`): each position is rated as if the driver looks
  along the camera axis, the "if you look there" worst case. This is the number stamped on that
  position's images.
- **Forward gaze, per study**, the headline number: every source from every position, placed in
  mount coordinates, counted once, rated against one fixed line of sight (the driver's view of the
  road) with the session L_b.

Forward gaze:

- The gaze direction is stored in the position profile as `[pan_deg, tilt_deg]` in mount
  coordinates (`line_of_sight` already accepts this form). The zero need not point forward, but it
  must be reproducible (§5.1), or the gaze direction means nothing in the next session.
- **De-duplication.** Frames overlap, so one source can be detected at several positions. Two
  detections are the same source when their angular footprints overlap in mount coordinates. The
  observation that lies wholly inside its frame and nearest the frame centre is kept (least
  vignetting and distortion). The others are recorded against it; the luminance difference between
  two views of the same source is a free consistency check.
- **Cut sources.** A source touching the frame edge has a truncated ω. It is flagged in its
  per-position result. In the forward-gaze rating a complete observation from another position
  replaces it; if there is none, the flag carries over.
- **Coverage.** Sources outside every frame are not counted. The study reports **glare-weighted
  coverage**: the fraction of the forward hemisphere the positions cover, each direction weighted
  by `1/p²`, i.e. by how much a source there would count. A gap just below the line of sight
  matters; a gap overhead at p ≈ 10 hardly does. Below `coverage_min` the forward-gaze UGR is
  flagged as a lower bound.
- The position profile editor can propose a tiling of the forward field with a set overlap, and
  shows the coverage map while positions are recorded.

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
measured `k` **validates** the value stored in the camera profile (§4.5) rather than replacing it. Drift beyond ±10%
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

### 4.5 Camera profiles

`k` is valid only for one body, lens and aperture, and the lens constants only for one lens at one
focal length. They travel together as a **camera profile**: body serial, lens name, focal length,
distortion `k1`, vignetting map, aperture, ISO, bracket, shutter model, `k` with the date it was
measured, and the spectral factors. Changing the lens means a new profile and a new calibration.

- Profiles are TOML files in the data directory (`profiles/camera/`, §6), so the web UI can edit
  them on the Pi. `config/analysis.toml` and `config/camera.toml` remain the template a new profile
  starts from. Every result stores the profile's name and content hash.
- On connect, the serial and lens name the body reports are compared with the selected profile. A
  mismatch is a warning in the UI and a flag on every result of that study.
- **Lens geometry measured with the head**: pan and tilt by known angles while tracking one small
  bright spot across the frame. Pixel position against angle gives the effective focal length and
  `k1` without a chart. Needs the gear ratios verified first (`docs/PIVOT_DETAIL.md` §8).
- **Vignetting**: a flat field at the working aperture, or the same spot-tracking run with a small
  uniform patch, measuring its relative luminance across the frame.

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

**Control service and API** (decided 2026-11-19; mount side built and sim-tested: `hal/mount.py`,
`service/`, `web/api.py`, `tools/rigctl.py`). The head is open-loop, so its position exists only
in the memory of the process that moved it, and only one process can hold the camera's USB
session. One long-running service therefore owns both. The web UI and the `rigctl` CLI are both
clients of its HTTP API; `mountctl` stays as the bench and measurement tool.

| Command | Behaviour |
|---|---|
| `jog_start(direction, speed)` / `jog_stop()` | Hold-to-move. Ramps up, runs at constant speed until stopped, ramps down. Soft limits apply: the ramp-down starts early enough to stop at the limit. A controlled stop keeps the step count exact, so the position stays certain |
| heartbeat | While a jog runs the client pings about every 100 ms; no ping for ~300 ms stops the jog. Phones drop WiFi or lock mid-press |
| `nudge(direction, deg)` | Fixed step; 0.1 / 1 / 5° in the UI |
| `goto(pan, tilt)` | Absolute move, backlash-compensated (as built) |
| `set_speed(fraction)` | Fraction of the configured maximum; `config/mount.toml` stays the ceiling |
| `stop()` | Controlled ramp-down of any motion; position stays certain |
| `estop()` | Immediate stop (the built `abort()`); position becomes an estimate, re-zero |
| `set_zero()` | Declare the current pose the datum ("save home") |
| `save_position(name)` | Re-approaches the current angle along `approach_sign`, then records it. A jog can arrive from either side but replay always arrives along `approach_sign`, so without this a position saved after a jog the other way replays off by the backlash |
| `status()` | Angles, enabled, zeroed, certain, moving, speed |

- Saving a position or setting the zero is refused where the backlash cannot be taken up (an
  axis on its soft limit with no room to back off): such a position would not replay.
- Before a zero exists, a single jog or nudge is capped at `unzeroed_jog_deg` (mount.toml): no
  soft limit can apply while the position is unknown, but the fiducial must be findable.
- After a service restart the head must be re-zeroed. The last position is persisted only as a
  hint: once the drivers are released the head can be moved by hand, or sag.
- **Position profiles**: an ordered list of named positions per vehicle configuration. Each
  position stores its angles, its masks (§3.1, §3.2) and a reference image taken from live view
  when it was saved. The profile stores the zero reference (where the fiducial sits in that cab, how
  the rig is placed at the eye point: seat position, height) and the forward gaze (§3.6). A
  position list is only reusable between sessions if its zero is.
- **Aim check on replay**: the frame captured at each position is compared with the stored
  reference image (FFT phase correlation in numpy). The offset in degrees is logged per position
  and flagged above a limit.

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
- **Live view** from the camera's own feed (gphoto2 preview, decided 2026-11-19), served to the
  browser as an MJPEG stream. It pauses during every bracket and calibration shot, which share the
  USB link, and stays off for the whole recording run: it warms the sensor, and the extra dark
  current shows in the long frames. While it is paused the UI shows capture progress and the last
  frame instead. Built 2026-11-19 (`service/liveview.py`): one grabber thread for all viewers,
  idle when nobody watches, live view ended on the body after 5 s idle and before every bracket;
  camera errors are shown and retried. On the desk a simulated cab follows the simulated head.
  The gphoto2 side is tested against a fake libgphoto2 only
- Live view overlays: crosshair at the optical axis, current pan/tilt, the current position's
  masks, and the outlines of the other saved positions projected into the view. Built, except the
  masks. The outlines use the nominal field of view from `config/analysis.toml`, so they are as
  good as the focal length there (MEASURE)
- Check the body's serial and lens name against the selected camera profile (§4.5)

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
- In `dark` studies, detect reflections in glazing regions (§3.1, §3.2)
- Per source: share of the position's total and the UGR change if the source were removed (§5.6)
- Process each position as soon as its bracket lands, in a worker process separate from the
  service, so jogging and live view stay responsive. Re-analysis from the raw archive writes a new
  result version and keeps the old one
- Study level: combine all positions into the forward-gaze rating (§3.6)

Target: under 60 s per direction end to end on a Pi 4.

### 5.4 Web interface

Runs headless on the Pi's own access point; used from a phone or laptop in the cab. FastAPI.

- **Fully offline.** The client has no internet on the Pi's network, so every script, style and
  font is served by the Pi: no CDN, no Node build step
- **Dark by default.** In a dark cab a bright screen adds light to the scene being measured and
  shows up as a reflection in the windscreen
- Several clients may be connected; all see the same state, and any of them can stop motion
- Live status everywhere: current position, capture progress, processing queue, errors (the
  movement page polls `/api/status` at 4 Hz, which doubles as the connection check)
- Long operations run as background jobs with progress pushed to the UI; the browser can
  disconnect and reconnect without losing a session
- Shut down the Pi cleanly from the UI. The OS and archive are on a microSD card, which corrupts
  if the supply is switched off under it

Three pages, decided 2026-11-19.

**Home: studies** (`/home`, built 2026-11-19 on the simulator: start recording with the session
sequence, step lights, automatic analysis, per-position cards with the luminance map, study
summary, library search, results zip, disk space. Not built: camera profile selection (§4.5),
re-analysis, the raw zip, the comparison view, the shutdown button, and the images of §5.5
other than the luminance map. Zero and calibration shot are recorded as skipped and flagged
until optical zeroing and the camera calibration page exist.)

- Select the position profile and the camera profile; set the lighting condition (§3.1), operator
  and notes
- **Start recording** runs the session sequence: zero check → calibration shot → hemisphere sweep →
  positions → back to a check position (drift and repeatability). Each step shows as a status
  light. A step can be skipped, which flags the study
- **Start analysis**: positions are analysed automatically as their brackets land; the button
  (re)runs the whole study, including the forward-gaze rating, from the raw archive
- Latest results: per-position cards with the images of §5.5, and the study summary
- Library of previous studies: search and filter by vehicle, configuration, date, profiles,
  operator, lighting condition, UGR range and flags. Comparison view: the same position across
  studies
- Downloads: a results zip (images, Excel, CSV, JSON) and a raw zip (CR2s), separately. The raw
  set is ~200 MB per position. Disk space indicator

**Movement calibration** (`/movement`, built 2026-11-19 except the tiling proposal, the coverage
map and profile copy; the aim check on replay is not built either)

Saving a position also stores a live-view frame as its reference image. The mask editor draws
on that image: tap the corners, tap the first one (or Finish) to close, choose Exclude, Glazing,
or Luminaire with a name and source type. Masks are validated, versioned
(`<profile>/<position>@<hash>`) and drawn over live view whenever the head is at that position.

On a portrait phone the live view is pinned to the top of the screen, so it stays in sight
while the arrows are pressed.

- Live view at all times (paused while capturing), with the overlays of §5.2
- Joystick: hold-to-move buttons per direction, nudge buttons with a step size, speed selector,
  stop and emergency stop (§5.1)
- Set zero ("save home"), aiming at the fiducial with the crosshair
- Record positions into a position profile: save (re-approaches, §5.1), name, reorder, delete, go
  to, retake the reference image. Propose a tiling for forward-gaze coverage; coverage map
- Position profiles: create, copy, edit; zero reference description; set the forward gaze by
  aiming at the road view and pressing "set forward gaze"
- Mask editor per position: exclusion, glazing and named luminaire ROIs (type, optional `reflects`),
  drawn on a captured frame

**Camera calibration**

- Camera profiles (§4.5): create, copy when the lens changes, edit; body and lens check
- Exposure settings: aperture, ISO, bracket and shutter model, against the choices the body offers
- Lens geometry: focal length and `k1` from the spot-tracking run; vignetting map
- Luminance calibration: aim at the target in live view, draw the ROI, enter the meter reading
  (luminance, or illuminance and ρ), shoot. Shows the measured `k` against the stored one and the
  drift; accept into the profile. `k` history per profile
- Spectral factor table per source type
- Geometry masks of the profile: solid angle per pixel, position index for the camera axis and for
  the forward gaze

### 5.5 Data and export

Every study retains:

- **Raw archive**: original CR2s, so any measurement can be recomputed later with different
  settings
- **Per-position images**, each stamped in a corner with that position's UGR (camera axis,
  labelled as such), category, position and study id, and a ⚠ mark when the result carries flags:
  1. **Photo**: the full-size JPEG embedded in the best-exposed CR2 of the bracket. Illustrative
     only; it carries the camera's own tone curve and white balance
  2. **Luminance map**: log false colour on a **fixed scale** by default, so the same colour is the
     same luminance in every study. Auto scale optional
  3. **Glare contribution map**: per pixel `L²ω/p²`, showing where the UGR comes from
  4. **Source map**: the photo with a box per source, labelled with id, L and share of the
     position's glare. Direct and reflected sources distinguished; dashed when flagged
  5. **UGR masks**: position-index contours and the exclusion, glazing and ROI polygons over the
     photo
- **Forward-gaze map** per study: every counted source in mount angles around the forward gaze,
  with position-index contours, the frame outlines and the uncovered area, stamped with the
  forward-gaze UGR
- **Excel workbook** (XlsxWriter, BSD, decided 2026-11-19). Sheets: Summary, Positions, Sources,
  Movement profile, Camera profile, Calibration & QA, Provenance. Contents in §5.6
- **CSV**: flat results tables, as now
- **PDF report**: the images and tables above, the calibration record, the zero drift and all
  parameters
- Full parameter provenance on every result, including the software version (git commit): a code
  change can change a number too

### 5.6 Metrics

Items not yet in the built `sources.csv` / `result.json` are marked *new*.

Per source

- Name, type, kind (`roi` / `blob`), *path (`direct` / `reflection`)*, *`reflects`*
- L mean (ω-weighted), *L median*, L peak, ω, σ, τ, p (camera axis *and forward gaze*),
  contribution `L²ω/p²`
- *Share of the position's total contribution*, and *ΔUGR if the source were removed*: what a
  design change to that one light would buy
- *Illuminance at the eye from the source* `E = L·ω·cos θ`, and *contrast* `L / L_b`
- Clipped fraction, flags (*including "touches frame edge"*)

Per position

- UGR (camera axis) and category; *contribution to the forward-gaze UGR*
- L_b and its method, number of sources (*direct / reflected*), max L
- *Luminance percentiles L50 / L90 / L99*, *fraction of the frame above threshold*
- *Threshold sensitivity*: UGR at 250 / 500 / 1000 / 2000 cd/m², showing how much the number
  depends on the chosen threshold
- Merge frame-to-frame ratios, clipped pixels, *aim error* (§5.1), flags

Per study

- ***Forward-gaze UGR*** and category: the headline
- Highest per-position UGR and where; number of positions above 19 and above 22
- *Glare-weighted coverage* (§3.6)
- L_b, E_ind and sweep coverage; lighting condition
- `k` measured against stored, and the drift; zero drift in degrees
- *ΔUGR at the check position, start against end*: a measured repeatability figure per session
- Flag count

UGR values are never averaged. The scale is logarithmic, and a mean of UGRs has no physical
meaning.

---

## 6. Data model

```
CameraProfile    name, body_serial, lens, focal_mm, distortion_k1, vignette_map_ref, aperture,
                 iso, bracket, shutter_model, k, k_measured_at, spectral_factors, content_hash
Vehicle          id, name, configuration, notes
PositionProfile  name, vehicle_id, zero_reference, forward_gaze (pan_deg, tilt_deg),
                 threshold_cd_m2, reflection_threshold_cd_m2, content_hash
Position         profile, order, id, name, pan_deg, tilt_deg, saved_at; files <id>.jpg
                 (reference image) and <id>.masks.json (exclude, glazing, include ROIs:
                 name, type, polygon, threshold; reflects planned)
Study            id, vehicle_id, position_profile, camera_profile, lighting_condition,
                 started_at, operator, notes, k_stored, k_measured, k_drift_flag, L_b, E_ind,
                 zero_ref, zero_drift_deg, check_delta_ugr, ugr_forward, coverage,
                 software_version, flags
Measurement      study_id, position, pan_deg, tilt_deg, bracket_dir, result_dir,
                 result_version, ugr, n_sources, aim_error_deg, params_snapshot
Source           measurement_id, name, kind, path (direct | reflection), reflects, L_cd_m2,
                 L_median, L_peak, omega_sr, p, p_forward, contribution, share, delta_ugr,
                 duplicate_of
CalibrationRec   camera_profile, k, target, meter_reading, measured_at
```

A study is one recording session; the UI calls it a study. Everything lives under one data
directory on the Pi. Profiles are TOML files under `profiles/camera/` and `profiles/positions/`
(each position's reference image and masks in a folder beside its profile, named by the
position's id, so a rename cannot orphan them), so the web UI can edit them and a result can
carry their hash. Profile and position names may not contain `/` or `\`: they are URL path
segments. Studies are directories under `studies/` holding the brackets and
results. SQLite indexes both for the library and search; the files remain the record, so the index
can always be rebuilt from them.

As built (`storage/studies.py`): `studies/<id>/study.json` (settings, steps, positions, results,
summary, flags, software commit), `profile.toml` and `masks/<position id>.json` frozen at the
start, so editing a profile later cannot change what a past study measured,
`brackets/{sweep_NN, NN_<id>, check_<id>}`, `results/<same>/` (`result.json`, `heatmap.png`,
`sources.csv`, `luminance.npy`) and `background.json`. The SQLite index is `index.sqlite`,
rebuilt from the folders at every server start. A study left recording or analysing by a server
that stopped is marked `interrupted` at the next start.

---

## 7. Module layout

```
ugr_rig/
├── hal/
│   ├── config.py         config/mount.toml → dataclasses                    (built)
│   ├── motion.py         trapezoidal profiles, merged two-axis pulse stream  (built)
│   ├── backend.py        pigpio backend + desk simulator                     (built)
│   ├── mount.py          limits, backlash, step accounting, wave streaming   (built)
│   │                     jog, nudge, speed, controlled stop, backlash state  (built)
│   ├── tmc2209.py        UART register layer: config, read-back, status      (next)
│   ├── camera_config.py  config/camera.toml, shutter arithmetic              (built)
│   ├── camera.py         gphoto2/PTP: settings, bracket capture, download,   (built, mock-tested)
│   │                     live view preview frames
│   └── camera_sim.py     synthetic cab scene → Bayer frames; simulated cab   (built)
│                         live view that follows the simulated head
├── photometry/
│   ├── profile.py        config/analysis.toml → dataclasses                  (built)
│   ├── linearise.py      CR2 → Bayer superpixels (rawpy), black level        (built)
│   ├── hdr.py            Debevec-weighted merge on linear data               (built)
│   ├── calibration.py    k model, target measurement, vignetting             (built)
│   └── masks.py          per-pixel direction + solid angle, Guth/Iwata p     (built)
├── analysis/
│   ├── detection.py      ROI + absolute-threshold source extraction          (built)
│   │                     glazing read, excluded and flagged for now          (built)
│   │                     reflections in glazing                              (planned)
│   ├── background.py     frame mean (provisional) + hemisphere sweep → L_b   (built)
│   ├── ugr.py            CIE 117 formula, perception scale                   (built)
│   └── forward.py        all positions: de-duplication, coverage, forward-gaze UGR (planned)
├── service/              rig controller: owns the mount and camera, one motion at a time, jog
│                         heartbeat, position profiles, live view; study.py:  (built)
│                         the session sequence and its analysis queue
├── storage/              study folders (the record) + SQLite index (search)  (built)
│                         exports beyond the results zip                      (planned)
├── report/               heatmap (built); photo, contribution map, source map, mask overlay,
│                         corner stamp, forward-gaze map, Excel (XlsxWriter), PDF
├── pipeline/             region.py: one direction end to end (built); study.py: worker-process
│                         jobs for a study (sweep, regions) and its summary   (built)
│                         forward gaze, optical zeroing                       (planned)
├── web/                  FastAPI over service/: HTTP API, MJPEG live view, movement and home
│                         pages (static/: plain HTML/CSS/JS, no build step)   (built)
│                         camera calibration page                             (planned)
└── tools/                mountctl, ugrctl: bench CLIs; rigctl: client of the service API (built)
```

Rationale: `hal` is the only layer that touches hardware, so the whole photometry and analysis
chain is testable off-rig against stored CR2 sets. That matters — most iterations will happen at
a desk, not in a truck.

`service/` is the only place that holds hardware state between requests. `web/` and `rigctl` go
through its API and never import `hal`; a CLI process that owned the mount itself would lose the
head's position when it exits.

Optical zeroing sits in `pipeline/` because it needs the mount and the camera together; `hal`
stays testable without either.

---

## 8. Suggested build order

Status per step: `README.md`.

Revised 2026-11-19. `photometry`, `analysis`, `hal/camera.py` and the `hal` mount are built and
sim-tested.

1. Camera on the bench: `ugrctl camera info`, one bracket, and live view over gphoto2. This is the
   most likely source of unpleasant surprises
2. `hal/tmc2209.py`, on the bench with a logic analyser, then on the built head: gear ratio, settle
   time, backlash, repeatability, run/hold current
3. At the desk, alongside 1–2, against the simulators: continuous jog in `hal/mount.py`,
   `service/` with its API, `rigctl`, preview frames in `hal/camera*.py` (built 2026-11-19)
4. `web` movement calibration page: needed in the cab to record positions, and handy for measuring
   the head (built 2026-11-19, incl. reference images and the mask editor; tiling, coverage
   map and the aim check still to come)
5. `storage` + the session sequence (`pipeline/study.py`), optical zeroing and the drift check;
   home page (built 2026-11-19 on the simulator, except optical zeroing and the drift check,
   which need the fiducial centroiding, and the calibration shot, which needs the camera page)
6. Reflections in detection, the forward-gaze combination, report images and the Excel workbook
7. Camera calibration page (lens geometry, `k`), validation of the photometry against the target
   and the handheld meter; PDF report

---

## 9. Open items

- Hemisphere sweep as built: a 3 x 3 grid around the forward gaze (columns ~57°, rows ~40°),
  shot with the full 8-frame bracket. §3.3 asks for a short bracket: the sweep only needs the
  background, so the shortest frames could be dropped there. Decide once the real capture time
  per bracket is known; 9 full brackets plus the moves may take several minutes on the body
- The heatmap figure is white (matplotlib default): too bright for the dark pages and the cab.
  Restyle it with the report images (§5.5), with a fixed luminance scale
- Masks are drawn on the live-view reference image and applied to the bracket frames, so live
  view must show the same field as the CR2. Confirm on the body that Canon live view is not
  cropped or offset relative to the raw frame; if it is, store the offset in the camera profile
- Reflections (§3.1, §3.2): the default reflection threshold and the double-image merge radius,
  both set from the first `dark` session in a cab
- Lights outside the vehicle seen through the glass at night: exclusion polygons, or a lights-off
  reference bracket per position (cab lights off, same pose) subtracted from the lights-on one,
  which isolates everything the cab's own lights produce, direct and reflected. Costs a second
  bracket per position. The same difference is the only way reflections could be measured in a
  `daylight` study
- Forward gaze (§3.6): how it is aimed per vehicle (e.g. a road point at a given distance),
  `coverage_min`, the extent of the forward field, and the footprint-overlap test for
  de-duplication
- Whether the hemisphere sweep and the forward-gaze tiling can be one capture set with the full
  bracket: saves a pass, costs time per sweep frame
- Proposed, not decided: **veiling luminance** (disability glare, Stiles–Holladay / CIE 146)
  against the forward gaze, computable from data already captured, and relevant at night with
  reflections near the line of sight; **small-source UGR** (CIE 146/147) using a distance per
  inclusion ROI measured once with a tape
- Lens constants for the EF-S 18–55 at 18 mm: effective focal length, radial distortion k1,
  vignetting map at f/8 — all `MEASURE` in `config/analysis.toml`
- Raw clip level of the 2000D at ISO 100 (`saturation_fraction`, currently a conservative 0.92)
- Vibration and settle time — needs measuring on the actual head
- Whether the illuminance sensor is added for L_b cross-check
- Spectral correction factor table — which source types matter in practice
- 2000D over gphoto2: libgphoto2 2.5.34 lists it with capture, config, trigger and preview. Still
  to confirm on the body: config names, that `capturetarget`, `iso`, `aperture` are writable, how
  `focusmode` reports the lens switch, the live view frame rate, and how preview hands over to
  capture
- Small sources (ω < 0.0003 sr, e.g. single indicator LEDs) are outside CIE 117 and flagged; CIE
  146/147 small-source correction needs the distance to the source, which the camera does not have
  (see the distance-per-ROI proposal above)
- Fiducial target design and where it is mounted in a cab (§5.1 zeroing)
- Hold current: how low it can go before the tilt axis creeps during a bracket
- Approved-supplier source for the USB-PD module (currently Amazon)
- Pi 4 processing time per direction: ~8 s on a desktop at full resolution, so an estimated
  30–45 s on the Pi 4 — measure against the 60 s target
