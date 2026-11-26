# Camera and processing

Status: 2026-11-20. Covers `hal/camera*.py`, `photometry/`, `analysis/`, `report/`,
`pipeline/region.py` and `tools/ugrctl.py`. Measurement model and requirements: `REQUIREMENTS.md`.

```
bracket (8 CR2) ─► superpixels ─► Debevec merge ─► × k ─► cd/m² map ─► sources ─► L_b ─► UGR
                                                                         │
                                       luminance.npy · sources.csv · images · result.json
```

---

## 1. Camera — Canon EOS 2000D

### 1.1 Body setup, once

The 2000D does not expose these over PTP, so they are set by hand and the code checks what it can.

| Setting | Value | Why | Checked by code |
|---|---|---|---|
| Mode dial | **M** | Shutter, aperture and ISO must be under software control | Yes — refuses to connect otherwise |
| Lens AF/MF switch | **MF**, focused | AF hunting in a dark cab blocks the shutter; refocusing moves the image | Warning only: `focusmode` read `One Shot`, then `Manual` once set to MF (2026-11-23) |
| Lens STABILIZER switch | **Off, taped** | The rig's lens is the IS II: a moving element can shift the image between frames and sessions | No |
| Zoom ring | **18 mm, taped** | Focal length enters every solid angle | No |
| Highlight Tone Priority | Off | Changes raw exposure and the ISO floor | Indirectly: EXIF ISO check fails |
| Long exp. noise reduction | Off | Doubles the 1 s and 4 s frames; data is fine either way | No |
| Exposure level increments | 1/3 stop (default) | The APEX shutter model assumes it | No |
| Auto power off | Disable | A sleeping body drops off USB | Code tries, and only warns if refused (the 2000D answered "busy"): set it by hand |

### 1.2 What the code sets

`config/camera.toml`: image format RAW, capture target (SD card by default), ISO 100, f/8, white
balance Daylight (metadata only), then the shutter per frame. Every bracket speed is checked
against the body's offered list at connect time, not halfway through a bracket.

### 1.3 Bracket

Fixed: 1/4000, 1/1000, 1/250, 1/60, 1/15, 1/4, 1, 4 s — 2 EV apart, shortest first, so if the
link drops the frames that protect the bright sources are already on the Pi. At f/8 / ISO 100 this
covers roughly 0.5 cd/m² (night background) to ~200 000 cd/m² (bare LED) unclipped.

**Shutter timing.** The labels are rounded: "1/60" is timed as 1/64 s, "1/4000" as 1/4096 s. The
merge uses the APEX value (`shutter_model = "apex"`). With the labels instead, adjacent frames
disagree by 3–7 % and the merge shows seams. Every merge reports the measured ratio between
adjacent frames (in `result.json`, flagged above 3 %), which is the running check that this holds
on the real shutter. A speed that is consistently off goes into `[exposure.shutter_correction]`.

### 1.4 Per-frame verification

A frame only counts once all of these pass; only then is the card copy deleted:

1. Downloaded size equals the size the camera reports, and is above `min_file_bytes`
2. EXIF shutter / f-number / ISO match the request (catches the dial off M, HTP on)
3. LibRaw decodes it

A failure raises `BracketError` and the card copy stays. A dropped link raises
`CameraDisconnected`; the bracket directory then has a manifest with no `finished_at`, and
processing refuses it — re-shoot the direction whole.

### 1.5 Errors

| Exception | Meaning |
|---|---|
| `CameraNotFound` | Nothing on USB — off, asleep, cable |
| `CameraBusy` | USB claimed by another process (gvfs on a desktop image), or the body stayed busy through 10 retries |
| `CameraSetupError` | Dial not on M, a setting not offered or not writable |
| `BracketError` | A frame failed verification |
| `CameraDisconnected` | Link lost mid-operation |

### 1.6 Bracket directory

```
frame_00.cr2 … frame_07.cr2
manifest.json      format, camera, settings snapshot, per frame: file, label, t_marked,
                   t_effective, aperture, ISO, bytes, sha256, EXIF; meta: region, pan, tilt
```

The simulator writes the same layout with `.npz` frames.

---

### 1.7 Live view

`Gphoto2Camera.preview()` returns one live-view JPEG (`capture_preview`); on a Canon EOS the first
call raises the mirror into live view, and `end_preview()` lowers it again through the
`viewfinder` / `eosviewfinder` toggle. `capture_bracket()` calls `end_preview()` before the first
frame, so a bracket is never shot from live view: live view warms the sensor, and the extra dark
current shows in the 1 s and 4 s frames.

The rig server owns the camera (`service/liveview.py`): one grabber thread serves every browser,
at up to 10 fps, only while someone watches; 5 s after the last viewer leaves it ends live view on
the body. Capture takes the camera through `LiveView.paused()`. Errors (dial off M, cable out,
asleep) show on the page and are retried every 2 s.

The pages pull frames one at a time (`/api/live/next?after=<seq>`, `liveFeed()` in
`web/static/api.js`): the next frame is asked for only when the last has arrived, so a slow WiFi
link lowers the frame rate instead of queueing frames. The MJPEG stream (`/api/live.mjpg`) the
pages used before fell ~10 s behind the head on the rig's own WiFi (2026-11-26), once
properly exposed frames made each JPEG several times larger; it is still served for other
clients. The movement page shows the camera's rate and what reaches the screen ("here … fps,
… s behind"): a lower rate there means the link is the limit. `/api/live.jpg` is one frame.

**Exposure.** With the dial on M the body shows live view at the brightness of the set exposure,
and nothing chose a shutter for it (4 s is left after every bracket). Live view was seen very
overexposed on the body (2026-11-25); this is the likely cause, inferred from the code and not yet
confirmed there. `hal/live_exposure.py` now meters the live-view frames and moves the **shutter
only**, between `shutter_fastest` and `shutter_slowest` of `[live_view]` in `camera.toml`: the frame
mean to `target_level`, and at most `highlight_fraction` of the frame clipped, whichever asks for
less. ISO and aperture stay the bracket's, and each bracket frame sets its own shutter, so no
result depends on it. The first frame of a live view waits up to 2.5 s for the exposure. The
movement page shows the shutter in use, "still too bright/dark" at the end of the range, and
"exposure not controlled" when the body refused the writes. `auto_exposure = false` turns it off.
If the body's own exposure simulation is off, the shutter has no effect on the picture and the
page ends at "still too bright" if it was white.

`FakeCamera.preview()` renders `CabView`, a night cab drawn on the sphere around the head, for
wherever the simulated head points, with a fiducial dot 6° right and 3° below the power-up pose
to practise zeroing on. It is illustrative only, not the bracket scene.

The tests use a fake libgphoto2. Live view has run on the body (2026-11-25); still to confirm there: its
frame size and rate, that `viewfinder` = 0 hands back to normal capture, that `shutterspeed` is
writable during live view and changes its brightness, and how many frames that takes
(`settle_frames`).

## 2. Processing

### 2.1 Linearise (`photometry/linearise.py`)

No demosaic: each 2×2 Bayer quad becomes one pixel, 6000 × 4000 → 3000 × 2000. Channels are
mixed with the Y row of the inverse of the camera's colour matrix (read from the CR2), normalised
to sum 1 — the closest V(λ) approximation this sensor gives. Clipping is judged per raw channel at
`saturation_fraction` of (white − black), before mixing.

### 2.2 Merge (`photometry/hdr.py`)

Debevec & Malik: `ln E = Σ w(Z)(g(Z) − ln Δt) / Σ w(Z)` with the hat weight. Raw is linear, so
`g = ln` and the response-curve recovery half of the method is not needed. Exposure normalisation
`N² / (t · ISO)` replaces `ln Δt`, so the output is in the units `k` expects. The hat is evaluated
on the brightest channel of each quad; clipped or below `noise_floor_fraction` gets zero weight.

Pixels clipped even in the shortest frame take that frame's value and are marked — a lower bound.

### 2.3 Calibration (`photometry/calibration.py`)

`L = k · DN · N² / (t · ISO) / vignette`. `k` comes from a bracket of the matt target:

```
python -m tools.ugrctl capture brackets/target --region target
python -m tools.ugrctl calibrate brackets/target --roi 0.45,0.45,0.55,0.55 --luminance 120
#   or  --lux 850 --rho 0.92        (L = ρE/π)
```

With the rig server, calibrate on the camera page (`/camera`) instead: aim at the target, drag a
box on it in live view, enter the meter reading, shoot. The page shows `k`, the drift from the
profile's stored value (over 10 % is a warning) and the uniformity inside the box (over 5 % is a
warning: the box is not fully on the target); **Accept** writes it into the camera profile, with
the old value kept in the profile's history. `k` lives in the camera profile
(`<data>/profiles/camera/<name>.toml`), not in `config/analysis.toml`, which only seeds the first
profile. A calibration accepted within 12 h counts as a study's calibration shot.

With no meter at all, choose "No meter: estimate from the ISO rating", aim at a neutral grey or
white surface (grey card, white wall) under daylight or white LED light, drag the box and shoot.
`k = 78 / (q · DN_sat)` (ISO 12232; `photometry/calibration.py` explains it), with q and the
saturation ISO ratio from `config/analysis.toml [calibration]` unless typed on the page. About
±30 %; accepted like a measurement, recorded as source `iso`, and studies on it are flagged.

Or type `k` on the same page ("Or type k"), with a note on where it comes from. It
replaces the stored value, whatever its source; the history keeps the old one. Studies using a
typed `k` skip the calibration step and are flagged "k typed by hand".

### 2.4 Geometry (`photometry/masks.py`)

Rectilinear 18 mm on the 2000D sensor at 3000 × 2000: 63.6 × 44.9°, 0.81 sr per frame. Each pixel
gets a direction and a solid angle; optional radial distortion `k1`. Mount convention: pan positive
= camera turns right, tilt positive = camera looks up.

Position index: Guth via Levin's fit at and above the line of sight, Iwata below it, capped at 16.
30° above → p 3.35, 30° below → 1.46, 30° to the side → 1.47.

### 2.5 Sources (`analysis/detection.py`)

1. Exclusion polygons (windows) — never sources
2. Inclusion ROIs — pixels inside at ≥ threshold form one named source; the ROI area is closed to
   blob detection
3. Everything else ≥ 500 cd/m² seeds a source: seeds merged within `merge_radius_px`, then grown
   to the whole luminaire. The source is every pixel connected to the seed (8 neighbours) at or
   above the higher of `extent_fraction` × threshold (250 cd/m², fixed so sessions compare) and
   `source_fraction` × the seed's own ω-weighted luminance (2/3: a bright lamp keeps its lit
   surroundings and the lens's glow out of its mean, which would otherwise dilute it). Seed
   pixels under that level are left out. Growth stops at exclusions, ROIs and glazing (and a
   reflection's at the edge of the glazing). Dropped below `min_pixels` after growing. A panel
   that straddles the threshold now counts whole; one wholly under it still does not.
   `extent_fraction = 1`, `source_fraction = 0` gives the plain threshold blobs of results
   before 2026-11-26.

Per source: ω-weighted mean L, ω, centre direction, p, contribution `L²ω/p²`, `n_seed_px`. Flags:
clipped (lower bound), ω outside 0.0003–0.1 sr, p capped, grown to more than
`extent_flag_ratio` (10, MEASURE) times its seed pixels (mostly under the threshold: check it is
one luminaire; an inclusion or exclusion area settles it).

Masks file, one per vehicle and direction, coordinates normalised 0..1 from the top left. The
mask editor on the movement page writes one per saved position
(`<data>/profiles/positions/<profile>/<id>.masks.json`, drawn on that position's reference
image), and the same file is the `--masks` input of `ugrctl process`:

```json
{
  "version": "truck-A/dash@1a2b3c4d",
  "exclude": [[[0.0, 0.0], [0.45, 0.0], [0.45, 0.30], [0.0, 0.30]]],
  "glazing": [[[0.0, 0.0], [1.0, 0.0], [1.0, 0.28], [0.0, 0.32]]],
  "include": [{"name": "dome", "type": "white_led",
               "polygon": [[0.62, 0.10], [0.74, 0.10], [0.74, 0.26], [0.62, 0.26]],
               "threshold_cd_m2": 500}]
}
```

`type` selects a spectral factor from `[calibration.spectral_factors]`. `glazing` marks the
windscreen and side glass. In a `dark` study (the default, or `--lighting dark`) what is bright
in it is a reflection of the cab's lights: detected against `reflection_threshold_cd_m2` with
`reflection_merge_radius_px` (laminated glass's double image), named R1, R2…, `path
"reflection"`, counted in UGR and drawn in cyan on the source map. In a `daylight` study glazing
is excluded like `exclude`. Either way glazing that is not a source counts toward L_b. A
luminaire ROI with `"reflects": "dome"` is the dome's reflection: the reflection threshold, and
the dome's type when the dome is drawn in the same position. A source touching the frame edge
is `cut` and flagged (its omega is truncated).

### 2.6 Background (`analysis/background.py`)

- **frame** (default until the sweep is in the session flow): solid-angle-weighted mean of the
  non-source pixels. Provisional — it depends on where the camera points — and flagged.
- **sweep**: brackets from several directions binned on a 2° grid around the line of sight,
  `E_ind = Σ L cos θ ΔΩ`, `L_b = E_ind / π`, scaled by the cosine-weighted coverage, which is
  reported. Window pixels count as background (`windows_in_background = true`) — they are not
  glare sources but they do light the eye.

```
python -m tools.ugrctl sweep brackets/sweep_* -o session_lb.json
python -m tools.ugrctl process brackets/dash --lb-json session_lb.json --masks masks/dash.json
```

### 2.7 UGR (`analysis/ugr.py`)

CIE 117: `UGR = 8 log₁₀(0.25 / L_b · Σ L²ω/p²)`, with the perception scale (10 imperceptible …
28 uncomfortable). By default each region is rated as if the observer looks along the camera axis
(`line_of_sight = "camera"`); `[pan, tilt]` fixes the gaze and positions sources relative to it.
The formula has no floor: small or dim sources give values under 10, even negative. They are
kept (so results still order) and flagged as under the scale, per position and for the forward
gaze. In the first full test (2026-11-26) ceiling panels at 300–600 cd/m² straddled the
500 cd/m² threshold, so only slivers of them counted, and every UGR came out negative; the
threshold sensitivity in each result shows this (UGR 4–9 at 250 cd/m²).

---

### 2.8 A whole study (`service/study.py`, `pipeline/study.py`)

The home page records a study of every position in a profile: checks, zero (on the fiducial:
the datum moves so the fiducial is (0, 0); failed and flagged when it is not found),
calibration shot ("done" when the camera
profile has a calibration accepted within 12 h, else skipped and flagged; `k` comes from the
profile, or the simulator's own when the profile is uncalibrated), a 3 x 3 hemisphere sweep around the forward gaze for
L_b (§2.6), each position (with an aim check: a live-view frame against the position's reference
image), the first position again as a repeatability check, and the drift check (the fiducial
again, not corrected; above 0.1° every angle is suspect). Live view is off for the whole
recording, apart from those few frames. Each bracket is analysed as it lands, in a worker process, sweep
first so every position gets the session L_b; the study's summary gives the highest UGR, how
many positions are over 19 and 22, and the check's UGR difference. Folder layout:
`REQUIREMENTS.md` §6.

## 3. Outputs per region

| File | Content |
|---|---|
| `luminance.npy` | float32 cd/m², 3000 × 2000 |
| `sources.csv` | One row per source: L, median, peak, ω, σ, τ, p, contribution, share, UGR change without it, illuminance at the eye, L/L_b, centroid, flags |
| `photo.jpg` | The CR2's embedded JPEG of the best-exposed frame (simulator: rendered from luminance) |
| `luminance.png` | Log false colour on the fixed `[report]` scale, clipped pixels magenta |
| `contribution.png` | Each source pixel's share of Σ L²ω/p²: where the UGR comes from |
| `sources.png` | The photo with a box per source (id, name, L, share; dashed if flagged), windows hatched, glazing outlined |
| `masks.png` | Position-index rings and the exclusion, glazing and luminaire polygons |
| `result.json` | All of the above plus metrics (percentiles, fraction above threshold, threshold sensitivity), flags, merge statistics and frame-to-frame ratios, geometry, and the full provenance: frame hashes, capture settings, k, weights, masks, analysis config |

Every image is dark and carries the position's UGR, category, position and study in a corner,
with a warning mark when the result has flags. A study adds `report.xlsx` (`report/excel.py`),
`report.pdf` (`report/pdf.py`), `forward.json` and `coverage.png` (the forward gaze). The home
page downloads the results zip (everything but the brackets and `.npy` arrays) and the raw zip
(the brackets, streamed); `rigctl study download ID results|raw|pdf|excel` does the same.

---

## 4. `ugrctl`

```
ugrctl --camera sim selftest            synthetic cab end to end, ~5 s, no hardware
ugrctl simulate DIR                     fake 2000D bracket + matching demo masks
ugrctl camera info                      what the body reports and offers
ugrctl capture DIR --region R --pan P --tilt T
ugrctl process DIR [--masks M] [--k K] [--lighting dark|daylight] [--lb L | --lb-json J]
ugrctl shoot SESSION --region R ...     capture + process
ugrctl calibrate DIR --roi x0,y0,x1,y1 (--luminance L | --lux E --rho ρ)
ugrctl sweep DIR... [-o J]
```

`--camera sim` on any capturing command uses the simulator.

---

## 5. Tested, and not

| | |
|---|---|
| Synthetic end to end (`selftest`) | k recovered to 0.01 %; background luminance median error 0.6 %; source luminances within 0.1 %; UGR equal to ground truth (17.32 vs 17.32); clipped LED flagged; window excluded; apex vs label timing seams detected |
| Camera control flow | Against a fake libgphoto2 (`tests/`): settings order, busy retry, verified-before-delete, wrong mode refused, truncated download refused, live view ended before the bracket |
| On the body (2026-11-23, on the Pi) | `ugrctl camera info`: connects, every `Gphoto2Camera.KEYS` name found, 1/3-stop shutter list with all 8 bracket speeds, f/8, ISO 100, RAW and memory card set. Which of those were written and which already stood was not recorded |
| **Not tested** | A bracket and the live-view shutter control on the real 2000D, real CR2 files, the real colour matrix, real shutter timing, processing speed on the Pi 4 (desktop: ~8 s per region at full size) |

## 6. Next on the body

1. `ugrctl camera info`: done 2026-11-23, no change to `Gphoto2Camera.KEYS`
2. One bracket of a static scene; check the frame-to-frame ratios in `result.json`
3. Raw clip level at ISO 100 → `saturation_fraction`
4. Focal length and k1 from a chart; vignetting map from a flat field at f/8
5. Target + meter → `k`
6. Time `process` on the Pi 4
