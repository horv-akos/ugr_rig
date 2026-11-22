#!/usr/bin/env python3
"""Camera, capture and UGR processing from the command line.

Run from the repository root:

    python -m tools.ugrctl selftest                     # synthetic end-to-end check, no hardware
    python -m tools.ugrctl simulate brackets/demo       # fake bracket of a demo cab scene
    python -m tools.ugrctl process brackets/demo --masks config/demo_masks.json --k 0.0064

    python -m tools.ugrctl camera info                  # what the 2000D reports and offers
    python -m tools.ugrctl capture brackets/dome --region dome --pan 20 --tilt 35
    python -m tools.ugrctl calibrate brackets/target --roi 0.45,0.45,0.55,0.55 --luminance 120
    python -m tools.ugrctl shoot sessions/test --region dash   # capture + process
    python -m tools.ugrctl sweep brackets/sweep_* --k 0.0064 -o lb.json

`--camera sim` swaps the 2000D for the simulator on any capturing command.
Processing never touches hardware.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import math
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from analysis import background, detection  # noqa: E402
from analysis import ugr as ugr_mod  # noqa: E402
from hal.camera import BracketManifest, create_camera  # noqa: E402
from hal.camera_config import apex_time, load_camera_config, nearest_choice, parse_shutter  # noqa: E402
from photometry.masks import CameraGeometry, camera_to_view, position_index  # noqa: E402
from photometry.profile import load_analysis_config  # noqa: E402
from pipeline import region  # noqa: E402

log = logging.getLogger("ugrctl")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def open_camera(args, scene=None):
    cfg = load_camera_config(args.camera_config)
    if args.camera == "sim":
        from hal.camera_sim import demo_scene

        cam = create_camera("sim", cfg, scene=scene or demo_scene(2000, 3000))
    else:
        cam = create_camera("gphoto2", cfg)
    cam.connect()
    return cam


def region_meta(args) -> dict:
    return {"region": args.region, "pan_deg": args.pan, "tilt_deg": args.tilt}


def parse_roi(text: str) -> tuple[float, float, float, float]:
    parts = [float(x) for x in text.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("ROI is x0,y0,x1,y1 in 0..1")
    return tuple(parts)


def load_lb(args) -> dict | None:
    if getattr(args, "lb_json", None):
        return json.loads(Path(args.lb_json).read_text())
    if getattr(args, "lb", None) is not None:
        return {"method": "given", "provisional": False, "L_b": float(args.lb)}
    return None


def print_result(res: region.RegionResult, out: Path) -> None:
    print(res.summary())
    for s in sorted(res.sources, key=lambda s: -s.contribution):
        print(f"  {s.id:>2} {s.name:<14} L {s.L_cd_m2:>11,.0f} cd/m2  "
              f"omega {s.omega_sr:.2e} sr  p {s.p:5.2f}  "
              f"{'  [' + '; '.join(s.flags) + ']' if s.flags else ''}")
    for f in res.flags:
        print(f"  ! {f}")
    print(f"  -> {out}  ({res.timing_s['total']:.1f} s)")


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


def cmd_camera_info(args) -> int:
    cam = open_camera(args)
    try:
        print(json.dumps(cam.info(), indent=2))
        if hasattr(cam, "choices"):
            for key, values in cam.choices().items():
                print(f"{key}: {', '.join(values)}")
    finally:
        cam.close()
    return 0


def cmd_capture(args) -> int:
    cam = open_camera(args)
    try:
        t0 = time.monotonic()
        m = cam.capture_bracket(args.out, meta=region_meta(args))
    finally:
        cam.close()
    print(f"{len(m.frames)} frames -> {args.out}  ({time.monotonic() - t0:.1f} s)")
    for f in m.frames:
        print(f"  {f.file}  {f.shutter_label:>7}  t={f.t_effective_s:.6g} s  {f.bytes / 1e6:.1f} MB")
    return 0


def cmd_simulate(args) -> int:
    from hal.camera_sim import DEMO_MASKS, demo_scene

    w, h = (int(x) for x in args.size.split("x"))
    args.camera = "sim"
    cam = open_camera(args, scene=demo_scene(h, w))
    try:
        cam.capture_bracket(args.out, meta=region_meta(args))
    finally:
        cam.close()
    masks_path = Path(args.out) / "demo_masks.json"
    masks_path.write_text(json.dumps(DEMO_MASKS, indent=2))
    print(f"simulated bracket -> {args.out}; masks -> {masks_path}")
    print(f"true k = {cam.k_true}")
    return 0


def with_lighting(cfg, args):
    import dataclasses

    if getattr(args, "lighting", None):
        cfg = dataclasses.replace(cfg, detection=dataclasses.replace(cfg.detection,
                                                                     lighting=args.lighting))
    return cfg


def cmd_process(args) -> int:
    cfg = with_lighting(load_analysis_config(args.analysis_config), args)
    masks = detection.RegionMasks.load(args.masks)
    out = Path(args.out) if args.out else Path(args.bracket) / "result"
    res = region.evaluate_region(args.bracket, out, cfg, masks=masks, lb=load_lb(args),
                                 k=args.k, name=args.name)
    print_result(res, out)
    return 0


def cmd_shoot(args) -> int:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bracket = Path(args.out) / f"{args.region}_{stamp}"
    cam = open_camera(args)
    try:
        cam.capture_bracket(bracket, meta=region_meta(args))
    finally:
        cam.close()
    cfg = with_lighting(load_analysis_config(args.analysis_config), args)
    res = region.evaluate_region(bracket, bracket / "result", cfg,
                                 masks=detection.RegionMasks.load(args.masks),
                                 lb=load_lb(args), k=args.k)
    print_result(res, bracket / "result")
    return 0


def cmd_calibrate(args) -> int:
    from photometry.calibration import target_luminance

    cfg = load_analysis_config(args.analysis_config)
    target = target_luminance(args.luminance, args.lux, args.rho)
    m = region.calibrate(args.bracket, cfg, args.roi, target)
    print(f"k = {m.k:.6g}   (target {m.target_cd_m2:.4g} cd/m2, ROI cv {100 * m.cv:.1f} %, "
          f"{m.n_px} px)")
    if m.drift is not None:
        print(f"stored k = {m.k_stored:.6g}, drift {100 * m.drift:+.1f} %"
              + ("  ** WARNING **" if m.drift_flag else ""))
    else:
        print("no stored k yet - put this value in config/analysis.toml [calibration] k")
    Path(args.bracket, "calibration.json").write_text(json.dumps(m.as_dict(), indent=2))
    return 0


def cmd_sweep(args) -> int:
    cfg = load_analysis_config(args.analysis_config)
    res = region.background_from_sweep(args.brackets, cfg, k=args.k)
    print(f"E_ind {res['E_ind_lx']:.3g} lx  ->  L_b {res['L_b']:.3g} cd/m2   "
          f"coverage {100 * res['coverage_cos_weighted']:.0f} % over {res['frames']} frames")
    if args.o:
        Path(args.o).write_text(json.dumps(res, indent=2))
    return 0


# --------------------------------------------------------------------------- #
# selftest
# --------------------------------------------------------------------------- #


def cmd_selftest(args) -> int:
    from hal.camera_sim import DEMO_MASKS, K_TRUE, FakeCamera, demo_scene

    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'ok  ' if ok else 'FAIL'} {name}{'  ' + detail if detail else ''}")
        if not ok:
            failures.append(name)

    print("shutter arithmetic")
    check("parse 1/4000", abs(parse_shutter("1/4000") - 2.5e-4) < 1e-12)
    check("apex 1/60 -> 1/64", abs(apex_time(1 / 60) - 1 / 64) < 1e-12)
    check("apex 0.3 -> 2^(-5/3)", abs(apex_time(0.3) - 2 ** (-5 / 3)) < 1e-12)
    check("nearest choice", nearest_choice(1 / 64, ["1/50", "1/60", "1/80"]) == "1/60")

    print("geometry")
    g = CameraGeometry(3000, 2000, 0.00744, 18.0)
    a, b = math.radians(g.fov_deg()[0] / 2), math.radians(g.fov_deg()[1] / 2)
    analytic = 4 * math.asin(math.sin(a) * math.sin(b))
    check("frame solid angle vs analytic", abs(g.solid_angle.sum() / analytic - 1) < 2e-3,
          f"{g.solid_angle.sum():.4f} vs {analytic:.4f} sr")
    up30 = np.array([0, math.sin(math.radians(30)), math.cos(math.radians(30))])
    check("Guth p on axis = 1", abs(float(position_index(np.array([0, 0, 1.0]))) - 1) < 1e-9)
    p_up, p_dn = float(position_index(up30)), float(position_index(up30 * [1, -1, 1]))
    check("Guth 30 deg above = 3.35", abs(p_up - 3.348) < 0.01, f"{p_up:.3f}")
    check("Iwata 30 deg below = 1.46", abs(p_dn - 1.462) < 0.01, f"{p_dn:.3f}")

    print("synthetic cab, end to end")
    cfg = load_analysis_config(args.analysis_config)
    h, w = 500, 750                                   # superpixels; raw 1500 x 1000
    pitch = 22.3 / (2 * w) * 1e3                      # keep the real 2000D field of view
    cfg = dataclasses.replace(cfg, sensor=dataclasses.replace(cfg.sensor, pixel_pitch_um=pitch),
                              lens=dataclasses.replace(cfg.lens, vignetting_map=""))
    scene = demo_scene(h, w)
    ccfg = load_camera_config(args.camera_config)
    tmp = Path(tempfile.mkdtemp(prefix="ugr_selftest_"))
    try:
        cam = FakeCamera(ccfg, scene=scene, seed=3)
        cam.connect()
        cam.capture_bracket(tmp / "bracket", meta={"region": "demo", "pan_deg": 0, "tilt_deg": 0})

        roi = (0.55, 0.45, 0.65, 0.55)                # plain interior wall
        sy, sx = slice(int(0.45 * h), int(0.55 * h)), slice(int(0.55 * w), int(0.65 * w))
        m = region.calibrate(tmp / "bracket", cfg, roi, float(scene.L[sy, sx].mean()))
        check("k recovered", abs(m.k / K_TRUE - 1) < 0.01, f"{m.k:.5g} vs {K_TRUE}")

        masks = detection.RegionMasks.load(_write(tmp / "masks.json", DEMO_MASKS))
        res = region.evaluate_region(tmp / "bracket", tmp / "out", cfg, masks=masks, k=m.k)
        L = np.load(tmp / "out" / "luminance.npy")

        any_src = np.zeros((h, w), bool)
        for msk in scene.sources.values():
            any_src |= msk
        bg = ~any_src & ~scene.exclude
        err_bg = np.median(np.abs(L[bg] / scene.L[bg] - 1))
        check("background luminance", err_bg < 0.03, f"median error {100 * err_bg:.2f} %")

        by_name = {}
        for s in res.sources:
            for nm, msk in scene.sources.items():
                cx, cy = (int(round(c)) for c in s.centroid_px)
                if msk[cy, cx]:
                    by_name[nm] = s
        check("all four sources found", set(by_name) == set(scene.sources),
              f"found {sorted(by_name)}; {len(res.sources)} total")
        check("dome came from its ROI", by_name.get("dome") is not None
              and by_name["dome"].kind == "roi")
        check("cluster (250 cd/m2) not a source", len(res.sources) == len(scene.sources))
        for nm in ("dome", "reading_led", "display"):
            s = by_name.get(nm)
            if s is None:
                continue
            truth = scene.notes["levels"][nm]
            check(f"L {nm}", abs(s.L_cd_m2 / truth - 1) < 0.03,
                  f"{s.L_cd_m2:,.0f} vs {truth:,.0f}")
        bl = by_name.get("bare_led")
        check("bare LED flagged clipped", bl is not None and bl.clipped_fraction > 0)
        check("window excluded", all(not scene.exclude[int(s.centroid_px[1]), int(s.centroid_px[0])]
                                     for s in res.sources))

        # UGR against ground truth, both without the clipped LED
        geom = CameraGeometry.from_config(cfg, (h, w))
        truth_sources, truth_labels = [], np.zeros((h, w), np.int32)
        for i, (nm, msk) in enumerate(scene.sources.items(), 1):
            vv, uu = np.nonzero(msk)
            truth_labels[msk] = i
            if nm == "bare_led":
                continue
            truth_sources.append(detection._describe(i, nm, "truth", None, vv, uu, scene.L,
                                                     geom.solid_angle, np.zeros((h, w), bool),
                                                     geom, 1.0))
        lb_true, _ = background.lb_frame(scene.L, geom.solid_angle, truth_labels, scene.exclude,
                                         cfg.background.windows_in_background)
        ugr_mod.rate_sources(truth_sources, camera_to_view(0, 0, "camera"), 3e-4, 0.1, 16)
        ugr_true = ugr_mod.ugr(truth_sources, lb_true)
        mine = [s for s in res.sources if s is not bl]
        ugr_mine = ugr_mod.ugr(mine, res.L_b)
        check("L_b (frame)", abs(res.L_b / lb_true - 1) < 0.03, f"{res.L_b:.3f} vs {lb_true:.3f}")
        check("UGR within 0.3", abs(ugr_mine - ugr_true) < 0.3,
              f"{ugr_mine:.2f} vs {ugr_true:.2f} (clipped LED left out of both)")
        check("outputs written", all((tmp / "out" / f).exists()
                                     for f in ("luminance.png", "sources.png", "photo.jpg",
                                               "result.json", "sources.csv")))
        ratios = [p["ratio"] for p in res.hdr_stats["pair_ratios"] if p["ratio"]]
        check("apex shutter model: frame steps consistent",
              all(abs(r - 1) < 0.01 for r in ratios), " ".join(f"{r:.3f}" for r in ratios))

        # the same bracket, timed with the marked labels instead
        nominal = tmp / "nominal"
        shutil.copytree(tmp / "bracket", nominal)
        man = BracketManifest.load(nominal)
        for f in man.frames:
            f.t_effective_s = f.t_marked_s
        man.save(nominal)
        rn = region.merge_bracket(nominal, cfg).hdr.pair_ratios
        worst = max(abs(p["ratio"] - 1) for p in rn if p["ratio"])
        check("nominal labels show the seams apex removes", worst > 0.03,
              f"worst step off {100 * worst:.1f} %")

        print(f"\n  {res.summary()}  [{res.timing_s['total']:.1f} s at {w}x{h}]")
        if args.keep:
            keep = Path(args.keep)
            shutil.copytree(tmp / "out", keep, dirs_exist_ok=True)
            print(f"  outputs kept in {keep}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\nselftest " + ("passed" if not failures else f"FAILED: {', '.join(failures)}"))
    return 0 if not failures else 1


def _write(path: Path, obj) -> Path:
    path.write_text(json.dumps(obj))
    return path


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ugrctl", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--camera", choices=["gphoto2", "sim"], default="gphoto2")
    p.add_argument("--camera-config", default=None, help="default config/camera.toml")
    p.add_argument("--analysis-config", default=None, help="default config/analysis.toml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def region_args(sp):
        sp.add_argument("--region", default="region")
        sp.add_argument("--pan", type=float, default=0.0, help="degrees, for the record")
        sp.add_argument("--tilt", type=float, default=0.0)

    def processing_args(sp):
        sp.add_argument("--masks", help="JSON with exclusion polygons and inclusion ROIs")
        sp.add_argument("--k", type=float, help="override the calibration constant")
        sp.add_argument("--lighting", choices=("dark", "daylight"),
                        help="dark: glazing is searched for reflections; daylight: glazing is "
                             "excluded (default: analysis.toml)")
        lb = sp.add_mutually_exclusive_group()
        lb.add_argument("--lb", type=float, help="background luminance, cd/m2")
        lb.add_argument("--lb-json", help="output of `ugrctl sweep -o`")

    cam = sub.add_parser("camera", help="camera queries")
    camsub = cam.add_subparsers(dest="camcmd", required=True)
    camsub.add_parser("info", help="model, mode, settings and offered choices").set_defaults(
        fn=cmd_camera_info)

    c = sub.add_parser("capture", help="shoot the configured bracket into a directory")
    c.add_argument("out")
    region_args(c)
    c.set_defaults(fn=cmd_capture)

    s = sub.add_parser("simulate", help="fake bracket of the demo cab scene")
    s.add_argument("out")
    s.add_argument("--size", default="3000x2000", help="superpixels WxH (raw is twice)")
    region_args(s)
    s.set_defaults(fn=cmd_simulate)

    pr = sub.add_parser("process", help="bracket directory -> UGR, sources, metrics, images")
    pr.add_argument("bracket")
    pr.add_argument("--out", help="default <bracket>/result")
    pr.add_argument("--name")
    processing_args(pr)
    pr.set_defaults(fn=cmd_process)

    sh = sub.add_parser("shoot", help="capture + process one region")
    sh.add_argument("out", help="session directory")
    region_args(sh)
    processing_args(sh)
    sh.set_defaults(fn=cmd_shoot)

    ca = sub.add_parser("calibrate", help="k from a bracket of the matt target")
    ca.add_argument("bracket")
    ca.add_argument("--roi", type=parse_roi, required=True, help="x0,y0,x1,y1 in 0..1")
    ca.add_argument("--luminance", type=float, help="meter reading, cd/m2")
    ca.add_argument("--lux", type=float, help="illuminance on the panel")
    ca.add_argument("--rho", type=float, help="panel reflectance (with --lux)")
    ca.set_defaults(fn=cmd_calibrate)

    sw = sub.add_parser("sweep", help="hemisphere sweep brackets -> E_ind, L_b")
    sw.add_argument("brackets", nargs="+")
    sw.add_argument("--k", type=float)
    sw.add_argument("-o", help="write the result as JSON (for --lb-json)")
    sw.set_defaults(fn=cmd_sweep)

    st = sub.add_parser("selftest", help="synthetic end-to-end check, no hardware")
    st.add_argument("--keep", help="copy the outputs here")
    st.set_defaults(fn=cmd_selftest)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
