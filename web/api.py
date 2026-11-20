"""HTTP API over the rig controller.

Every client (the web pages, `rigctl`) goes through here; nothing else in the process touches
the mount. Motion calls return as soon as the motion has started, with its id, so the client
can keep pinging a jog or send a stop. Pass `"wait": true` to block until the motion ends
instead (what `rigctl` does for goto and nudge).

The interactive docs pages (/docs, /redoc) are switched off: they load their scripts from a
CDN, and the Pi's own WiFi has no internet. `/openapi.json` still describes every route. The
pages are plain HTML, CSS and JavaScript under `web/static/`, served as they are: no build step.

Live view is `/api/live.mjpg`, an MJPEG stream any `<img>` can show; `/api/live.jpg` is one
fresh frame.

Errors come back as `{"error": <type>, "detail": <message>}`:

    404  unknown profile or position
    409  the rig cannot do it right now: busy, no zero, soft limit, drivers off, aborted
    422  the request itself is wrong: bad direction, speed out of range, bad name
    503  no live-view frame (no camera, or it is not answering)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, AsyncIterator, Literal

from fastapi import FastAPI, Request
from fastapi.responses import (FileResponse, JSONResponse, RedirectResponse, Response,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from hal.mount import MountError
from service.controller import Motion, RigController, RigError
from service.positions import ProfileError, ProfileNotFound
from storage import StudyNotFound

STATIC = Path(__file__).resolve().parent / "static"
BOUNDARY = "frame"

Direction = Literal["left", "right", "up", "down"]


def Speed():  # noqa: N802 - reads like the type it constrains
    return Field(None, gt=0, le=1, description="fraction of the configured top speed")


class JogStart(BaseModel):
    direction: Direction
    speed: float | None = Speed()


class Nudge(BaseModel):
    direction: Direction
    deg: float = Field(1.0, gt=0, le=90)
    speed: float | None = Speed()
    wait: bool = False


class Goto(BaseModel):
    pan: float
    tilt: float
    speed: float | None = Speed()
    wait: bool = False


class SpeedBody(BaseModel):
    fraction: float = Field(gt=0, le=1)


class Wait(BaseModel):
    wait: bool = False
    speed: float | None = Speed()


class NewProfile(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    vehicle: str = Field("", max_length=200)
    zero_reference: str = Field("", max_length=2000)


class EditProfile(BaseModel):
    vehicle: str | None = Field(None, max_length=200)
    zero_reference: str | None = Field(None, max_length=2000)


class SavePosition(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    wait: bool = False


class Order(BaseModel):
    names: list[str]


class StartStudy(BaseModel):
    profile: str = Field(min_length=1, max_length=80)
    camera_profile: str | None = Field(None, max_length=80)
    lighting: Literal["dark", "daylight"]
    operator: str = Field("", max_length=80)
    notes: str = Field("", max_length=2000)
    sweep: bool = True
    check: bool = True


class NewCameraProfile(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    copy_from: str | None = Field(None, max_length=80)


class Calibrate(BaseModel):
    profile: str = Field(min_length=1, max_length=80)
    roi: list[float] = Field(min_length=4, max_length=4)
    luminance: float | None = Field(None, gt=0)
    lux: float | None = Field(None, gt=0)
    rho: float | None = Field(None, gt=0, le=1)
    method: Literal["meter", "iso"] = "meter"
    q: float | None = Field(None, ge=0.2, le=1)            # iso: ISO 12232 lens factor
    iso_ratio: float | None = Field(None, ge=0.25, le=4)   # iso: saturation ISO / label
    wait: bool = False


class Accept(BaseModel):
    calibration: str = Field(min_length=1, max_length=120)


class TypedK(BaseModel):
    k: float = Field(gt=0, lt=10)
    note: str = Field(min_length=1, max_length=300)


class Run(BaseModel):
    dwell_s: float = Field(0.0, ge=0, le=600)
    speed: float | None = Speed()
    wait: bool = False


def create_app(rig: RigController) -> FastAPI:
    app = FastAPI(title="UGR rig", version="0.6", docs_url=None, redoc_url=None)
    app.state.rig = rig

    def error(status: int):
        async def handler(request: Request, exc: Exception) -> JSONResponse:
            return JSONResponse(status_code=status,
                                content={"error": type(exc).__name__, "detail": str(exc)})
        return handler

    # Most specific first: ProfileNotFound is a ProfileError is a ValueError.
    app.add_exception_handler(ProfileNotFound, error(404))
    app.add_exception_handler(ProfileError, error(422))
    app.add_exception_handler(RigError, error(409))
    app.add_exception_handler(MountError, error(409))
    app.add_exception_handler(ValueError, error(422))
    app.add_exception_handler(StudyNotFound, error(404))

    def motion(m: Motion | None, wait: bool = False) -> dict[str, Any]:
        if m is None:
            return {"motion": None, "status": rig.status()}
        summary = rig.wait(m) if wait else m.summary()
        return {"motion": summary, "status": rig.status()}

    # -- pages -------------------------------------------------------------- #

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/home")

    @app.get("/home", include_in_schema=False)
    def home_page() -> FileResponse:
        return FileResponse(STATIC / "home.html", headers={"Cache-Control": "no-cache"})

    @app.get("/camera", include_in_schema=False)
    def camera_page() -> FileResponse:
        return FileResponse(STATIC / "camera.html", headers={"Cache-Control": "no-cache"})

    @app.get("/movement", include_in_schema=False)
    def movement_page() -> FileResponse:
        return FileResponse(STATIC / "movement.html", headers={"Cache-Control": "no-cache"})

    # -- status and power --------------------------------------------------- #

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        return rig.status()

    @app.get("/api/camera/geometry")
    def geometry() -> dict[str, Any]:
        return rig.geometry

    # -- live view ----------------------------------------------------------- #

    @app.get("/api/live.jpg")
    def live_jpg() -> Response:
        frame = rig.live.snapshot(timeout=5.0)
        if frame is None:
            return JSONResponse(status_code=503, content={
                "error": "NoFrame", "detail": rig.live.status()["error"] or
                f"no live-view frame (camera: {rig.live.source})"})
        return Response(frame, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/api/live.mjpg")
    async def live_mjpg(request: Request, frames: int | None = None) -> StreamingResponse:
        """MJPEG stream of the latest frames. `frames` ends it after that many (tests)."""

        async def stream() -> AsyncIterator[bytes]:
            rig.live.subscribe()
            try:
                seq, sent = -1, 0
                while frames is None or sent < frames:
                    if await request.is_disconnected():
                        break
                    s, frame = rig.live.latest()
                    if frame is None or s == seq:
                        await asyncio.sleep(0.02)
                        continue
                    seq, sent = s, sent + 1
                    yield (f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                           f"Content-Length: {len(frame)}\r\n\r\n").encode() + frame + b"\r\n"
            finally:
                rig.live.unsubscribe()

        return StreamingResponse(stream(), headers={"Cache-Control": "no-store"},
                                 media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}")

    @app.post("/api/mount/enable")
    def enable() -> dict[str, Any]:
        return rig.enable()

    @app.post("/api/mount/disable")
    def disable() -> dict[str, Any]:
        return rig.disable()

    @app.post("/api/mount/speed")
    def speed(body: SpeedBody) -> dict[str, Any]:
        return rig.set_speed(body.fraction)

    # -- motion ------------------------------------------------------------- #

    @app.post("/api/mount/zero")
    def zero(body: Wait | None = None) -> dict[str, Any]:
        return motion(rig.set_zero(), bool(body and body.wait))

    @app.post("/api/mount/backlash")
    def backlash(body: Wait | None = None) -> dict[str, Any]:
        return motion(rig.take_up_backlash(), bool(body and body.wait))

    @app.post("/api/mount/jog/start")
    def jog_start(body: JogStart) -> dict[str, Any]:
        return motion(rig.jog_start(body.direction, body.speed))

    @app.post("/api/mount/jog/ping")
    def jog_ping() -> dict[str, Any]:
        return {"jogging": rig.jog_ping()}

    @app.post("/api/mount/jog/stop")
    def jog_stop() -> dict[str, Any]:
        return motion(rig.jog_stop())

    @app.post("/api/mount/nudge")
    def nudge(body: Nudge) -> dict[str, Any]:
        return motion(rig.nudge(body.direction, body.deg, body.speed), body.wait)

    @app.post("/api/mount/goto")
    def goto(body: Goto) -> dict[str, Any]:
        return motion(rig.goto(body.pan, body.tilt, body.speed), body.wait)

    @app.post("/api/mount/stop")
    def stop() -> dict[str, Any]:
        return motion(rig.stop())

    @app.post("/api/mount/estop")
    def estop() -> dict[str, Any]:
        return motion(rig.estop())

    # -- camera profiles and calibration ---------------------------------------- #

    def cam_view(p) -> dict[str, Any]:
        d = p.as_dict()
        d["active"] = p.name == rig.camera_profile().name
        return d

    @app.get("/api/camera/profiles")
    def camera_profiles() -> list[dict[str, Any]]:
        active = rig.camera_profile().name
        out = []
        for n in rig.camera_profiles.names():
            p = rig.camera_profiles.load(n)
            out.append({"name": p.name, "k": p.k, "k_measured_at": p.k_measured_at,
                        "k_source": p.k_source,
                        "lens_name": p.lens_name, "focal_mm": p.focal_mm,
                        "aperture": p.aperture, "active": p.name == active})
        return out

    @app.post("/api/camera/profiles", status_code=201)
    def new_camera_profile(body: NewCameraProfile) -> dict[str, Any]:
        return cam_view(rig.camera_profiles.create(body.name, body.copy_from))

    @app.get("/api/camera/profiles/{name}")
    def camera_profile(name: str) -> dict[str, Any]:
        return cam_view(rig.camera_profiles.load(name))

    @app.patch("/api/camera/profiles/{name}")
    def edit_camera_profile(name: str, body: dict[str, Any]) -> dict[str, Any]:
        """Edit lens, exposure, body, spectral factors. k changes only by calibration."""
        p = rig.camera_profiles.load(name).edited(body)
        rig.camera_profiles.save(p)
        return cam_view(p)

    @app.delete("/api/camera/profiles/{name}")
    def delete_camera_profile(name: str) -> dict[str, Any]:
        if len(rig.camera_profiles.names()) <= 1:
            raise RigError("the last camera profile cannot be deleted")
        return {"moved_to": rig.camera_profiles.delete(name)}

    @app.post("/api/camera/profiles/{name}/activate")
    def activate_camera_profile(name: str) -> dict[str, Any]:
        return cam_view(rig.set_active_camera_profile(name))

    @app.post("/api/camera/profiles/{name}/body")
    def take_body(name: str) -> dict[str, Any]:
        """Fill the profile's body serial and lens name from what the camera reports now."""
        info = rig.camera_info()["info"]
        p = rig.camera_profiles.load(name).edited({"body_serial": str(info.get("serial") or ""),
                                                   "lens_name": str(info.get("lens") or "")})
        rig.camera_profiles.save(p)
        return cam_view(p)

    @app.get("/api/camera/profiles/{name}/geometry.png")
    def profile_geometry(name: str) -> Response:
        import dataclasses

        from photometry.masks import CameraGeometry
        from photometry.profile import load_analysis_config
        from report.images import geometry_png

        p = rig.camera_profiles.load(name)
        cfg = load_analysis_config()
        cfg = dataclasses.replace(cfg, lens=dataclasses.replace(
            cfg.lens, focal_mm=p.focal_mm, distortion_k1=p.distortion_k1))
        b = cfg.processing.binning
        geom = CameraGeometry.from_config(cfg, (cfg.sensor.height_px // b, cfg.sensor.width_px // b))
        return Response(geometry_png(geom, cfg.ugr.position_index_cap), media_type="image/png")

    @app.get("/api/camera/info")
    def camera_info() -> dict[str, Any]:
        return rig.camera_info()

    @app.post("/api/camera/calibrate")
    def calibrate(body: Calibrate) -> dict[str, Any]:
        m = rig.calibrate(body.profile, body.roi, body.luminance, body.lux, body.rho,
                          body.method, body.q, body.iso_ratio)
        return motion(m, body.wait)

    @app.get("/api/camera/calibrations")
    def calibrations(profile: str | None = None) -> list[dict[str, Any]]:
        return rig.calibrations(profile)

    @app.post("/api/camera/profiles/{name}/accept")
    def accept(name: str, body: Accept) -> dict[str, Any]:
        return cam_view(rig.accept_calibration(name, body.calibration))

    @app.post("/api/camera/profiles/{name}/k")
    def typed_k(name: str, body: TypedK) -> dict[str, Any]:
        """Type k in by hand, with where it comes from. Replaces the stored k."""
        return cam_view(rig.set_k(name, body.k, body.note))

    # -- studies ------------------------------------------------------------- #

    @app.get("/api/studies")
    def studies(q: str = "") -> list[dict[str, Any]]:
        """The library: newest first, filtered by `q` over profile, vehicle, operator,
        notes, lighting and status."""
        return rig.list_studies(q)

    @app.post("/api/studies", status_code=201)
    def start_study(body: StartStudy) -> dict[str, Any]:
        m, sid = rig.start_study(body.profile, body.lighting, body.operator, body.notes,
                                 body.sweep, body.check, body.camera_profile)
        return {"study": sid, "motion": m.summary()}

    @app.get("/api/studies/{sid}")
    def get_study(sid: str) -> dict[str, Any]:
        return rig.study(sid)

    @app.post("/api/studies/{sid}/reanalyse")
    def reanalyse(sid: str) -> dict[str, Any]:
        """Analyse the recorded brackets again; the previous results go to previous/."""
        return rig.reanalyse(sid)

    @app.get("/api/studies/{sid}/files/{path:path}")
    def study_file(sid: str, path: str) -> FileResponse:
        return FileResponse(rig.studies.file(sid, path))

    @app.get("/api/studies/{sid}/results.zip")
    def study_zip(sid: str) -> Response:
        return Response(rig.studies.results_zip(sid), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{sid}.zip"'})

    # -- position profiles -------------------------------------------------- #

    @app.get("/api/positions")
    def profiles() -> list[dict[str, Any]]:
        return rig.list_profiles()

    @app.post("/api/positions", status_code=201)
    def new_profile(body: NewProfile) -> dict[str, Any]:
        return rig.describe(rig.create_profile(body.name, body.vehicle, body.zero_reference))

    @app.get("/api/positions/{profile}")
    def get_profile(profile: str) -> dict[str, Any]:
        return rig.describe(rig.get_profile(profile))

    @app.delete("/api/positions/{profile}")
    def delete_profile(profile: str) -> dict[str, Any]:
        return {"moved_to": rig.delete_profile(profile)}

    @app.post("/api/positions/{profile}/save")
    def save_position(profile: str, body: SavePosition) -> dict[str, Any]:
        return motion(rig.save_position(profile, body.name), body.wait)

    @app.post("/api/positions/{profile}/order")
    def reorder(profile: str, body: Order) -> dict[str, Any]:
        return rig.describe(rig.reorder_positions(profile, body.names))

    @app.post("/api/positions/{profile}/gaze")
    def gaze(profile: str) -> dict[str, Any]:
        return rig.describe(rig.set_forward_gaze(profile))

    @app.post("/api/positions/{profile}/run")
    def run(profile: str, body: Run | None = None) -> dict[str, Any]:
        body = body or Run()
        return motion(rig.run_profile(profile, body.dwell_s, body.speed), body.wait)

    @app.patch("/api/positions/{profile}")
    def edit_profile(profile: str, body: EditProfile) -> dict[str, Any]:
        return rig.describe(rig.update_profile(profile, body.vehicle, body.zero_reference))

    @app.get("/api/positions/{profile}/{position}/image")
    def position_image(profile: str, position: str) -> Response:
        jpeg = rig.position_image(profile, position)
        if jpeg is None:
            return JSONResponse(status_code=404, content={
                "error": "NoImage", "detail": f"{position!r} has no reference image yet"})
        return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-cache"})

    @app.post("/api/positions/{profile}/{position}/image")
    def retake_image(profile: str, position: str) -> dict[str, Any]:
        """Replace the reference image with live view; the head must be at the position."""
        return rig.describe(rig.retake_image(profile, position))

    @app.get("/api/positions/{profile}/{position}/masks")
    def get_masks(profile: str, position: str) -> dict[str, Any]:
        return rig.position_masks(profile, position)

    @app.put("/api/positions/{profile}/{position}/masks")
    def put_masks(profile: str, position: str, body: dict[str, Any]) -> dict[str, Any]:
        """Exclusion, glazing and luminaire polygons, normalised 0..1 from the top left.
        Validated and versioned on save; the file is what `ugrctl process --masks` reads."""
        return rig.set_position_masks(profile, position, body)

    @app.delete("/api/positions/{profile}/{position}")
    def delete_position(profile: str, position: str) -> dict[str, Any]:
        return rig.describe(rig.delete_position(profile, position))

    @app.post("/api/positions/{profile}/{position}/goto")
    def goto_position(profile: str, position: str, body: Wait | None = None) -> dict[str, Any]:
        body = body or Wait()
        return motion(rig.goto_position(profile, position, body.speed), body.wait)

    return app
