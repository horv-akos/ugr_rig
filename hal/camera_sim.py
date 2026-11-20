"""Desk simulator for the camera: renders a known luminance scene into Bayer
frames the way the 2000D would, and writes a bracket directory in the same
layout as the real camera (manifest.json + one file per frame).

Frames are .npz rather than CR2 - LibRaw cannot be fed a synthetic CR2 - but
they carry the same fields `photometry.linearise` takes from a CR2: mosaic,
CFA layout, per-channel black level, white level.

The scene is grey (all four Bayer channels equal), so any luminance weights
that sum to 1 recover it exactly. The shutter is timed in true APEX steps, so
running the merge with `shutter_model = "nominal"` shows the seams that the
apex model removes.

Live view is a separate, illustrative scene: a cab drawn on the sphere around
the head (`CabView`), rendered for wherever the simulated head points, so the
movement page can be used at a desk. It includes a fiducial dot off-centre from
the power-up pose, to practise zeroing on. It is not the bracket scene, and its
pixel values mean nothing photometrically.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .camera import BracketManifest, FrameRecord, now_iso, sha256_file
from .camera_config import CameraConfig, apex_time, effective_time, parse_shutter

# Normalised polygons matching demo_scene, in the format analysis.detection reads.
DEMO_MASKS = {
    "version": "demo-1",
    "exclude": [[[0.0, 0.0], [0.45, 0.0], [0.45, 0.30], [0.0, 0.30]]],
    "include": [{"name": "dome", "type": "white_led",
                 "polygon": [[0.62, 0.10], [0.74, 0.10], [0.74, 0.26], [0.62, 0.26]]}],
}

# 2000D-like sensor model at ISO 100
BLACK = 2048
WHITE = 15000
GAIN_E_PER_DN = 2.0
READ_NOISE_DN = 6.0
K_TRUE = 0.0064        # cd/m2 per (DN * N^2 / (t * ISO)); ~200 000 cd/m2 clips at 1/4096, f/8
CFA = np.array([[0, 1], [3, 2]], dtype=np.uint8)   # R G / G2 B, LibRaw channel indices
COLOR_DESC = "RGBG"
# Canon-like XYZ -> camera matrix, illustrative only (the real one comes from the CR2).
# Present so the simulator exercises the same weighting path as a real frame.
CAM_XYZ = np.array([[0.6939, -0.1016, -0.0866],
                    [-0.4428, 1.2473, 0.2177],
                    [-0.1175, 0.2178, 0.6162]])


@dataclass
class SyntheticScene:
    """Luminance in cd/m2 at superpixel resolution, plus ground truth."""

    L: np.ndarray                                   # (h, w) float64
    sources: dict[str, np.ndarray] = field(default_factory=dict)   # name -> bool mask
    exclude: np.ndarray | None = None               # window mask
    notes: dict[str, Any] = field(default_factory=dict)


def _disk(h: int, w: int, cx: float, cy: float, r: float) -> np.ndarray:
    yy, xx = np.mgrid[0:h, 0:w]
    return (xx - cx) ** 2 + (yy - cy) ** 2 <= r ** 2


def _rect(h: int, w: int, x0: float, y0: float, x1: float, y1: float) -> np.ndarray:
    m = np.zeros((h, w), bool)
    m[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)] = True
    return m


def demo_scene(h: int = 1000, w: int = 1500, seed: int = 1,
               bare_led_cd_m2: float = 1.5e6) -> SyntheticScene:
    """A dim cab at dusk: graded interior, a bright window (to be masked out),
    a dome light, a reading LED, a display, an instrument cluster below the
    threshold, and one bare LED far too bright for the darkest frame."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w] / np.array([h, w])[:, None, None]
    L = 3.0 + 12.0 * (1 - yy) * (0.6 + 0.4 * xx)                 # 3..15 cd/m2
    L *= 1 + 0.02 * rng.standard_normal((h, w)).clip(-3, 3)      # surface texture

    window = np.zeros((h, w), bool)
    window[: int(0.30 * h), : int(0.45 * w)] = True
    L[window] = 3000.0 + 1500.0 * xx[window]

    s = min(h, w)
    sources = {
        "dome": _disk(h, w, 0.68 * w, 0.18 * h, 0.035 * s),
        "reading_led": _disk(h, w, 0.86 * w, 0.32 * h, 0.006 * s + 1.5),
        "display": _rect(h, w, 0.36, 0.58, 0.56, 0.74),
        "bare_led": _disk(h, w, 0.92 * w, 0.86 * h, 2.5),
    }
    levels = {"dome": 8000.0, "reading_led": 60000.0, "display": 900.0, "bare_led": bare_led_cd_m2}
    for name, m in sources.items():
        L[m] = levels[name]
    cluster = _rect(h, w, 0.08, 0.72, 0.30, 0.82)
    L[cluster] = 250.0                                           # below 500: not a source

    return SyntheticScene(
        L=L, sources=sources, exclude=window,
        notes={"levels": levels, "cluster_cd_m2": 250.0, "k_true": K_TRUE,
               "expect_clipped": ["bare_led"]},
    )


# --------------------------------------------------------------------------- #
# live view
# --------------------------------------------------------------------------- #


def rotation(pan_deg: float, tilt_deg: float) -> np.ndarray:
    """Camera frame -> cabin frame. Same convention as photometry.masks.rotation
    (pan positive = right, tilt positive = up); repeated here so hal does not
    import the layers above it."""
    p, t = np.radians(pan_deg), np.radians(tilt_deg)
    z = np.array([np.sin(p) * np.cos(t), np.sin(t), np.cos(p) * np.cos(t)])
    x = np.array([np.cos(p), 0.0, -np.sin(p)])
    return np.stack([x, np.cross(z, x), z], axis=1)


class CabView:
    """A night cab on the sphere around the head, angles relative to the power-up
    pose: windscreen and side glass, A-pillars, dash, instrument cluster, a display,
    dome and reading lights, and a fiducial target.

    `render` draws it for live view (with a faint 10 deg grid); `luminance` gives the
    same scene in cd/m2 for brackets, so a simulated study gets a different, consistent
    result at every position. Lights at or above the 500 cd/m2 threshold: the display,
    the dome and the reading lights; the cluster, dials and fiducial stay below it.
    """

    FIDUCIAL = (6.0, -3.0)   # az, el: the operator jogs here and sets the zero
    DISPLAY = (18, 34, -22, -10)            # az0, az1, el0, el1
    CLUSTER = (-14, 10, -24, -14)
    DIALS = ((-7.5, -19), (3.5, -19))       # ring centres; radii 3.6 .. 4.2 deg
    LIGHTS = ((0, 58, 5.0), (-28, 48, 1.6), (28, 48, 1.6))   # dome, reading x2: az, el, r
    # cd/m2 for `luminance`: a cab at night with its own lights on
    CD = {"display": 900.0, "cluster": 120.0, "dial": 350.0, "dash": 1.5,
          "lights": (8000.0, 30000.0, 30000.0), "halo": 40.0, "fiducial": 400.0}

    def __init__(self, fov_deg: tuple[float, float] = (63.6, 44.9),
                 size: tuple[int, int] = (720, 480)) -> None:
        self.fov_deg = fov_deg
        self.size = size
        w, h = size
        tx = np.tan(np.radians(fov_deg[0]) / 2)
        ty = np.tan(np.radians(fov_deg[1]) / 2)
        u = (np.arange(w) + 0.5) / w * 2 - 1
        v = 1 - (np.arange(h) + 0.5) / h * 2
        x, y = np.meshgrid(u * tx, v * ty)
        rays = np.stack([x, y, np.ones_like(x)], axis=-1)
        self._rays = (rays / np.linalg.norm(rays, axis=-1, keepdims=True)).reshape(-1, 3)
        self._rays = self._rays.astype(np.float32)
        self._cache: tuple[tuple, bytes] | None = None

    def _sky(self, pan_deg: float, tilt_deg: float):
        """az, el per pixel (degrees), plus the two shape tests on them."""
        d = self._rays @ rotation(pan_deg, tilt_deg).T.astype(np.float32)
        az = np.degrees(np.arctan2(d[:, 0], d[:, 2]))
        el = np.degrees(np.arcsin(np.clip(d[:, 1], -1, 1)))

        def dist2(az0: float, el0: float) -> np.ndarray:
            # squared angular distance on az/el, scaled for convergence toward the pole:
            # exact enough for an illustration, and cheap. No az wrap needed: every
            # round feature sits well away from az = +-180.
            da = (az - np.float32(az0)) * np.float32(np.cos(np.radians(el0)))
            de = el - np.float32(el0)
            return da * da + de * de

        def box(a0: float, a1: float, e0: float, e1: float) -> np.ndarray:
            return (az >= a0) & (az <= a1) & (el >= e0) & (el <= e1)

        aa = np.abs(az)
        glass = ((aa <= 55) & (el >= -8) & (el <= 22)) | (
            (aa >= 62) & (aa <= 115) & (el >= -4) & (el <= 26))
        return az, el, dist2, box, glass

    def render(self, pan_deg: float, tilt_deg: float) -> np.ndarray:
        """RGB uint8 image (h, w, 3) seen with the head at (pan, tilt). Float32 and one
        az/el pass per frame: about 20 ms at 720 x 480 on a desktop."""
        w, h = self.size
        az, el, dist2, box, glass = self._sky(pan_deg, tilt_deg)
        img = np.empty((az.shape[0], 3), np.float32)
        img[:] = (np.clip(0.10 + 0.0012 * el, 0.04, 0.2)[:, None]
                  * np.array([1.0, 0.95, 0.9], np.float32))                # trim and roof
        img[el < -8] = (0.07, 0.07, 0.08)                                  # dash
        img[glass] = ((0.05 + 0.004 * np.clip(el[glass] + 8, 0, 40))[:, None]
                      * np.array([0.55, 0.7, 1.0], np.float32))            # night sky
        img[box(*self.DISPLAY)] = (0.75, 0.85, 1.0)
        img[box(*self.CLUSTER)] = (0.10, 0.16, 0.12)
        for a, e in self.DIALS:
            r2 = dist2(a, e)
            img[(r2 <= 4.2 ** 2) & (r2 >= 3.6 ** 2)] = (0.35, 0.9, 0.55)
        grid = ((np.abs(az * 0.1 - np.rint(az * 0.1)) < 0.012)
                | (np.abs(el * 0.1 - np.rint(el * 0.1)) < 0.012))       # 10 deg lines
        img[grid] = img[grid] * 0.6 + 0.08
        for a0, e0, r in self.LIGHTS:
            r2 = dist2(a0, e0)
            halo = r2 <= (r * 2.2) ** 2
            img[halo] = img[halo] * 0.5 + 0.25
            img[r2 <= r * r] = (1.0, 0.98, 0.92)
        r2 = dist2(*self.FIDUCIAL)
        img[r2 <= 1.2 ** 2] = (0.95, 0.95, 0.95)
        img[r2 <= 0.9 ** 2] = (0.0, 0.0, 0.0)
        img[r2 <= 0.35 ** 2] = (1.0, 1.0, 1.0)
        return (np.clip(img, 0, 1) * 255).astype(np.uint8).reshape(h, w, 3)

    def luminance(self, pan_deg: float, tilt_deg: float) -> np.ndarray:
        """The same scene in cd/m2, float64 (h, w): what a bracket at (pan, tilt) sees."""
        w, h = self.size
        cd = self.CD
        az, el, dist2, box, glass = self._sky(pan_deg, tilt_deg)
        L = np.clip(2.0 + 0.05 * (el + 20), 1.0, 6.0).astype(np.float64)   # trim and roof
        L[el < -8] = cd["dash"]
        L[glass] = 0.3 + 0.02 * np.clip(el[glass] + 8, 0, 40)              # night outside
        L[box(*self.DISPLAY)] = cd["display"]
        L[box(*self.CLUSTER)] = cd["cluster"]
        for a, e in self.DIALS:
            r2 = dist2(a, e)
            L[(r2 <= 4.2 ** 2) & (r2 >= 3.6 ** 2)] = cd["dial"]
        for (a0, e0, r), level in zip(self.LIGHTS, cd["lights"]):
            r2 = dist2(a0, e0)
            L[r2 <= (r * 2.2) ** 2] += cd["halo"]
            L[r2 <= r * r] = level
        r2 = dist2(*self.FIDUCIAL)
        L[r2 <= 1.2 ** 2] = cd["fiducial"]
        L[r2 <= 0.9 ** 2] = 0.5
        L[r2 <= 0.35 ** 2] = cd["fiducial"]
        return L.reshape(h, w)

    def jpeg(self, pan_deg: float, tilt_deg: float, quality: int = 80) -> bytes:
        """Encoded frame. The head is still most of the time, so the last frame is
        reused while the pose has not changed."""
        key = (round(pan_deg, 4), round(tilt_deg, 4), quality)
        if self._cache is not None and self._cache[0] == key:
            return self._cache[1]
        from PIL import Image, ImageDraw

        im = Image.fromarray(self.render(pan_deg, tilt_deg))
        ImageDraw.Draw(im).text((8, self.size[1] - 18), "SIMULATED VIEW", fill=(255, 190, 60))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality)
        self._cache = (key, buf.getvalue())
        return self._cache[1]


class FakeCamera:
    """Implements hal.camera.Camera against a SyntheticScene (brackets) and a
    CabView (live view).

    `pose` returns the simulated head's physical (pan, tilt) relative to its
    power-up pose, so live view follows the head. Without it the view is fixed.
    With `pose` and `bracket_view`, brackets are shot of the cab too
    (`CabView.luminance` at the bracket view's size) instead of the fixed demo scene.
    """

    def __init__(self, config: CameraConfig, scene: SyntheticScene | None = None,
                 seed: int = 0, k_true: float = K_TRUE, vignette: np.ndarray | None = None,
                 pose: Callable[[], tuple[float, float]] | None = None,
                 view: CabView | None = None, bracket_view: CabView | None = None) -> None:
        self.config = config
        self.scene = scene if scene is not None else demo_scene()
        self.k_true = k_true
        self.vignette = vignette
        self.pose = pose
        self.view = view
        self.bracket_view = bracket_view
        self._rng = np.random.default_rng(seed)
        self._connected = False
        self.previewing = False

    def connect(self) -> None:
        self._connected = True

    def close(self) -> None:
        self._connected = False

    def __enter__(self) -> "FakeCamera":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def info(self) -> dict[str, Any]:
        if self.pose and self.bracket_view:
            w, h = self.bracket_view.size
        else:
            h, w = self.scene.L.shape
        return {"model": "FakeCamera (2000D model)", "serial": "SIM", "lens": "ideal 18 mm",
                "sensor_px": f"{2 * w}x{2 * h}", "connected": self._connected}

    def render(self, t_true: float, aperture: float, iso: int,
               L: np.ndarray | None = None) -> np.ndarray:
        L = self.scene.L if L is None else L
        if self.vignette is not None:
            L = L * self.vignette
        dn = L * t_true * iso / (self.k_true * aperture ** 2)
        dn = dn + np.sqrt(np.maximum(dn, 0) / GAIN_E_PER_DN) * self._rng.standard_normal(dn.shape)
        mosaic = np.repeat(np.repeat(dn, 2, axis=0), 2, axis=1)
        mosaic += READ_NOISE_DN * self._rng.standard_normal(mosaic.shape)
        return np.clip(np.rint(mosaic + BLACK), 0, WHITE).astype(np.uint16)

    def preview(self) -> bytes:
        if not self._connected:
            raise RuntimeError("not connected")
        if self.view is None:
            self.view = CabView()
        self.previewing = True
        pan, tilt = self.pose() if self.pose else (0.0, 0.0)
        return self.view.jpeg(pan, tilt)

    def end_preview(self) -> None:
        self.previewing = False

    def apply_config(self, config: CameraConfig) -> None:
        self.config = config

    def capture_bracket(self, out_dir: str | Path,
                        meta: dict[str, Any] | None = None) -> BracketManifest:
        if not self._connected:
            raise RuntimeError("not connected")
        self.end_preview()
        cfg = self.config
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        manifest = BracketManifest(format="npz", camera=self.info(), settings=cfg.snapshot(),
                                   started_at=now_iso(), meta=dict(meta or {}))
        scene_L = None
        if self.pose and self.bracket_view:   # shoot the cab where the head points
            scene_L = self.bracket_view.luminance(*self.pose())
        for i, label in enumerate(sorted(cfg.exposure.shutter, key=parse_shutter)):
            t_true = apex_time(parse_shutter(label))
            mosaic = self.render(t_true, cfg.exposure.aperture, cfg.exposure.iso, scene_L)
            name = f"frame_{i:02d}.npz"
            np.savez(out / name, mosaic=mosaic, cfa=CFA, color_desc=COLOR_DESC,
                     black=np.full(4, BLACK, np.float64), white=float(WHITE), cam_xyz=CAM_XYZ)
            manifest.frames.append(FrameRecord(
                file=name, shutter_label=label, t_marked_s=parse_shutter(label),
                t_effective_s=effective_time(label, cfg.exposure.shutter_model,
                                             cfg.exposure.shutter_correction),
                aperture=cfg.exposure.aperture, iso=cfg.exposure.iso,
                bytes=(out / name).stat().st_size, sha256=sha256_file(out / name),
                captured_at=now_iso(),
                exif={"exposure_time_s": parse_shutter(label), "f_number": cfg.exposure.aperture,
                      "iso": cfg.exposure.iso, "model": "FakeCamera"},
            ))
        manifest.finished_at = now_iso()
        manifest.save(out)
        return manifest
