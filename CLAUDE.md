# CLAUDE.md

Raspberry Pi 4 rig that measures discomfort glare (CIE 117 UGR) in truck cabins. A Canon EOS
2000D on a stepper pan-tilt head shoots HDR brackets per direction. The Pi turns each bracket into
a calibrated luminance map, glare sources and a UGR value, and serves a web UI over its own WiFi.
The rig exists to **compare cabins and configurations**, so repeatability between sessions beats
absolute accuracy, and every result carries the parameters that produced it. Each position gets a
UGR along the camera axis; the study's headline is a forward-gaze UGR over all positions, and
reflections of the cab's lights in the glazing count as sources.

## Read first

| Doc | What's in it |
|---|---|
| `REQUIREMENTS.md` | Measurement model, calibration, functional requirements (motion API, web pages, outputs, metrics), data model, open items |
| `docs/CAMERA_AND_PROCESSING.md` | Camera control, bracket, merge, calibration, detection, UGR, `ugrctl` |
| `hal/README.md` | Mount driver: use, sim testing, pigpiod setup |
| `docs/PIVOT_DETAIL.md`, `docs/CONNECTIONS.md` | Head spec and wiring (hardware docs) |

Purchasing docs (`SHOPPING_LIST.md`, `SOURCING.md`, BOM spreadsheets) and superseded docs are
kept outside this repo.

## Commands

Run everything from the repo root. Python 3.11+ (uses `tomllib`).

```
pip install -r requirements.txt
python -m pytest -q tests/                      # all tests, ~15 s, no hardware
python -m tools.ugrctl --camera sim selftest    # synthetic cab end to end
python -m tools.mountctl --backend sim selftest # mount logic against the pulse simulator
python -m tools.rigctl selftest                 # rig server + API + client, private sim server, ~8 s
python -m web                                   # rig server, sim head + camera; /movement page
python -m web --host 0.0.0.0                    # same, reachable from the LAN; prints its URLs
python -m tools.ugrctl simulate /tmp/demo && python -m tools.ugrctl process /tmp/demo --masks /tmp/demo/demo_masks.json --k 0.0064
```

Run all three selftests and pytest before calling a change done.

## Layout

```
hal/         ONLY layer that touches hardware
  config.py motion.py backend.py mount.py    pan-tilt head (pigpio step/dir) + SimBackend;
                                             jog, nudge, speed, stop() ramp-down, backlash state
  camera_config.py camera.py camera_sim.py   2000D over python-gphoto2 + FakeCamera;
                                             preview()/end_preview() live view, CabView sim
  tmc2209.py                                 NOT WRITTEN YET - next hardware task
photometry/  profile (analysis.toml) · linearise (CR2 -> superpixels) · hdr (Debevec merge)
             · calibration (k) · masks (pixel direction/solid angle, Guth/Iwata position index)
analysis/    detection (ROIs, threshold blobs) · background (L_b) · ugr
             planned: glazing/reflections in detection, forward.py (all positions, forward gaze)
report/      heatmap.png renderer. Photo, maps, source boxes, stamp, Excel, PDF not started
pipeline/    region.py: one direction, bracket dir -> result dir. study.py not started
service/     controller.py: owns the mount and camera, one motion thread at a time, jog
             dead-man watchdog · liveview.py: one grabber thread for all viewers, paused()
             · positions.py: position profiles as TOML in <data>/profiles/positions/, with
             each position's reference image and masks beside them.
             Camera, session sequence, job queue not started
storage/     not started: data directory (profiles/, studies/), SQLite index
web/         api.py: FastAPI over service/ (/api/...); __main__.py runs it (`python -m web`)
             and prints the reachable URLs (netinfo.py).
             static/: movement page + mask editor (masks.js); plain HTML/CSS/JS ES modules,
             no build step, no CDN.
             Home and camera calibration pages not started
config/      mount.toml · camera.toml · analysis.toml (also the template for camera profiles)
tools/       mountctl.py (head bench CLI) · ugrctl.py (camera + processing CLI)
             · rigctl.py (HTTP client of the rig server, stdlib only)
tests/       pytest
```

Data flow: `capture_bracket()` writes `frame_NN.cr2` + `manifest.json` into a directory, and
`pipeline.region.evaluate_region()` reads only that directory. That is the seam: processing never
talks to hardware, so it is always testable on a desk against stored brackets.

## Rules

- **Code follows hardware.** Don't design around hardware that hasn't been decided. If a change
  needs a hardware decision, stop and ask.
- **`hal` is the only hardware layer.** Hardware imports (`pigpio`, `gphoto2`) happen inside the
  class that uses them, so every module still imports on a desk. Every hardware class has a
  simulator (`SimBackend`, `FakeCamera`). Keep it that way for anything new (e.g. `tmc2209`).
- **No GPL dependencies.** LGPL is fine (python-gphoto2, libgphoto2). PyTmcStepper was rejected
  for this reason.
- **Every physical constant lives in `config/*.toml` or a camera profile** (TOML in the data
  directory), not in code. Values marked `MEASURE` are placeholders until measured on the real
  hardware. Don't "fix" them with guesses.
- **The service owns the hardware.** Only `service/` holds a mount or camera between requests;
  `web/` and `rigctl` go through its API and never import `hal`. The head is open-loop, so a
  process that exits loses its position, and only one process can hold the camera's USB session.
- **Do not move the real head** until `hal/tmc2209.py` exists and verifies the driver config over
  UART. Without it, PAN runs 1/8 and TILT 1/32 microstepping.
- **Pi 4 only.** pigpio has no Pi 5 support.
- **Provenance:** anything that can change a number (threshold, k, lens constants, masks version,
  frame hashes) goes into `result.json`. New parameters go into `AnalysisConfig.snapshot()`
  automatically when added to the dataclasses.
- **Flag, don't drop.** Out-of-range or doubtful values (clipped pixels, small sources, provisional
  L_b, shutter ratio off) get flags in the result. They are not silently filtered.
- **Fail loudly and recoverably.** Typed exceptions (`CameraError` / `MountError` subclasses).
  A partial bracket has no `finished_at` and processing must refuse it.
- **Docs move with the code.** When behaviour changes, update `REQUIREMENTS.md`, the relevant
  doc in `docs/` and the status tables in `README.md` in the same change.

## Conventions that bite

- Mount angles: **pan positive = camera turns right, tilt positive = camera looks up.** Camera
  frame: x right, y up, z along the optical axis; image rows run downwards.
- Mask/ROI polygons are **normalised 0..1, origin top left**, so they survive a binning change.
- Shutter: labels are rounded. `"1/60"` is timed as 1/64 s, and the merge uses `t_effective_s`
  (APEX model), never `t_marked_s`.
- Working resolution is the 2×2 Bayer superpixel (3000 × 2000). Geometry pitch = raw pitch × binning.
- Position index: sources **below** the line of sight have lower p (count more) than above.
- Two UGRs: per position along the camera axis (stamped on that position's images), per study
  along the forward gaze (the headline). Never average UGR values; the scale is logarithmic.
- Glazing polygons are not exclusions. In `dark` studies sources inside them are reflections and
  count, against their own lower threshold; in `daylight` studies glazing is excluded. Until
  reflection detection exists, `exclusion_mask()` excludes glazing and the result flags it.
- Masks belong to a position (`<data>/profiles/positions/<slug>/<id>.masks.json`), drawn on its
  reference image `<id>.jpg`; the file is the `--masks` input of `ugrctl process`. Position ids
  are stable, so re-saving a position keeps its masks. Names may not contain `/` or `\`.
- Saving a position or setting the zero takes the backlash up along `approach_sign` first
  (`take_up_backlash()`), or replay lands off by the backlash. The mount tracks the last
  direction per axis; `status()["backlash_ok"]` shows it.
- `stop()` (or a `stop` event) is a controlled ramp-down, position stays certain. `abort()` is
  immediate, position becomes an estimate. Pass motions your own `threading.Event` so a stop
  sent before the move starts is not lost.
- Motion refusals (soft limit, no zero, drivers off, busy) are checked before the worker thread
  starts, so the API returns them as 409 instead of "started".
- Live view and capture share one USB link: anything that shoots takes the camera through
  `LiveView.paused()`, and `capture_bracket()` ends live view itself before the first frame.
- Pages are dark on purpose (a bright screen lights the cab and reflects in the glazing), and
  build DOM with `el()` from `web/static/api.js`, never `innerHTML` with operator-typed names.
- `k` = cd/m² per (DN · N² / (t · ISO)). `k = 0` means uncalibrated, and `process` refuses
  without `--k`.
- Hardware CLIs follow `mountctl` style: argparse subcommands, `--backend/--camera sim`, a
  `selftest` that checks against known answers.

## Style

Python with type hints, `from __future__ import annotations`, frozen dataclasses for config,
`typing.Protocol` for backends, numpy (float32 for image-sized arrays; the Pi has 8 GB, but keep one
decoded raw frame in memory at a time). Module docstrings explain *why* (the physics or hardware
reason), not just what. Plain, terse comments. No new heavy dependencies (OpenCV, pandas) without
asking: the Pi installs from wheels on a microSD card. Approved: XlsxWriter for the Excel export
(2026-11-19); FastAPI + uvicorn for the server (httpx for its tests). The web frontend is fully
offline: no CDN, no Node build step (so FastAPI's /docs pages are switched off).

## Status (2026-11-19)

Built and sim-tested: mount (incl. jog, nudge, speed, controlled stop, backlash tracking), the
rig server (`service/` + `web/api.py`) and `rigctl`, live view (simulated cab on the desk), the
movement calibration page at `/movement` with reference images and the mask editor, glazing
masks read by detection (excluded and flagged for now), camera control (fake libgphoto2 only), the whole
processing chain for one position, heatmap, `ugrctl`. Not yet run on the real 2000D, on real
CR2s, or on the Pi.

Plan v0.6 is recorded in `REQUIREMENTS.md` (decided 2026-11-19): live view from the camera's own
feed, motor control service with hold-to-move jog and position profiles, three web pages,
windscreen reflections, forward-gaze study rating, report images, metrics, Excel.

Next, roughly in order (`REQUIREMENTS.md` §8): camera on the body (`camera info`, one bracket,
live view); `hal/tmc2209.py`; at the desk meanwhile, `storage/` + session sequence + home
page; reflections, forward gaze (with the tiling and coverage map), report images, Excel; camera
calibration page, PDF. Open items list: `REQUIREMENTS.md` §9.
