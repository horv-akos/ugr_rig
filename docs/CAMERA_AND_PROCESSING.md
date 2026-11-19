# Camera and processing

Status: 2026-10-13. Covers `hal/camera*.py`, `photometry/`, `analysis/`, `report/heatmap.py`,
`pipeline/region.py` and `tools/ugrctl.py`. Measurement model and requirements: `REQUIREMENTS.md`.

```
bracket (8 CR2) ─► superpixels ─► Debevec merge ─► × k ─► cd/m² map ─► sources ─► L_b ─► UGR
                                                                         │
                                       luminance.npy · sources.csv · heatmap.png · result.json
```

---

## 1. Camera — Canon EOS 2000D

### 1.1 Body setup, once

The 2000D does not expose these over PTP, so they are set by hand and the code checks what it can.

| Setting | Value | Why | Checked by code |
|---|---|---|---|
| Mode dial | **M** | Shutter, aperture and ISO must be under software control | Yes — refuses to connect otherwise |
| Lens AF/MF switch | **MF**, focused | AF hunting in a dark cab blocks the shutter; refocusing moves the image | Warning only (reporting varies by body) |
| Zoom ring | **18 mm, taped** | Focal length enters every solid angle | No |
| Highlight Tone Priority | Off | Changes raw exposure and the ISO floor | Indirectly: EXIF ISO check fails |
| Long exp. noise reduction | Off | Doubles the 1 s and 4 s frames; data is fine either way | No |
| Exposure level increments | 1/3 stop (default) | The APEX shutter model assumes it | No |
| Auto power off | Disable | A sleeping body drops off USB | Code tries to set it |

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
asleep) show on the page and are retried every 2 s. `/api/live.mjpg` is the stream,
`/api/live.jpg` one frame.

`FakeCamera.preview()` renders `CabView`, a night cab drawn on the sphere around the head, for
wherever the simulated head points, with a fiducial dot 6° right and 3° below the power-up pose
to practise zeroing on. It is illustrative only, not the bracket scene.

Tested against a fake libgphoto2 only. Still to confirm on the body: that preview works, its
frame size and rate, and that `viewfinder` = 0 hands back to normal capture.

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

The first time, put the printed `k` into `config/analysis.toml`. After that the session value is
compared to the stored one and drift above 10 % is a warning.

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
3. Everything else ≥ 500 cd/m²: blobs, merged within `merge_radius_px`, dropped below `min_pixels`

Per source: ω-weighted mean L, ω, centre direction, p, contribution `L²ω/p²`. Flags: clipped
(lower bound), ω outside 0.0003–0.1 sr, p capped.

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
windscreen and side glass: in a dark study reflections of the cab's lights in it are to be
detected as sources (REQUIREMENTS §3.1). That is not built yet, so for now glazing is excluded
like `exclude`, still counts toward L_b, and every result with glazing carries a flag saying so.

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

---

## 3. Outputs per region

| File | Content |
|---|---|
| `luminance.npy` | float32 cd/m², 3000 × 2000 |
| `sources.csv` | One row per source: L, peak, ω, σ, τ, p, contribution, centroid, flags |
| `heatmap.png` | Log false-colour luminance, sources outlined and numbered, windows hatched, clipped pixels magenta, UGR and L_b in the title, top sources listed |
| `result.json` | All of the above plus flags, merge statistics and frame-to-frame ratios, geometry, and the full provenance: frame hashes, capture settings, k, weights, masks, analysis config |

---

## 4. `ugrctl`

```
ugrctl --camera sim selftest            synthetic cab end to end, ~5 s, no hardware
ugrctl simulate DIR                     fake 2000D bracket + matching demo masks
ugrctl camera info                      what the body reports and offers
ugrctl capture DIR --region R --pan P --tilt T
ugrctl process DIR [--masks M] [--k K] [--lb L | --lb-json J]
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
| **Not tested** | The real 2000D, real CR2 files, the real colour matrix, real shutter timing, processing speed on the Pi 4 (desktop: ~8 s per region at full size) |

## 6. Next on the body

1. `ugrctl camera info` — confirm config names and writability; adjust `Gphoto2Camera.KEYS` if needed
2. One bracket of a static scene; check the frame-to-frame ratios in `result.json`
3. Raw clip level at ISO 100 → `saturation_fraction`
4. Focal length and k1 from a chart; vignetting map from a flat field at f/8
5. Target + meter → `k`
6. Time `process` on the Pi 4
