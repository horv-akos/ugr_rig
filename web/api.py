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


class SavePosition(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    wait: bool = False


class Order(BaseModel):
    names: list[str]


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

    def motion(m: Motion | None, wait: bool = False) -> dict[str, Any]:
        if m is None:
            return {"motion": None, "status": rig.status()}
        summary = rig.wait(m) if wait else m.summary()
        return {"motion": summary, "status": rig.status()}

    # -- pages -------------------------------------------------------------- #

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/movement")  # the home page comes with storage/

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

    # -- position profiles -------------------------------------------------- #

    @app.get("/api/positions")
    def profiles() -> list[dict[str, Any]]:
        return rig.list_profiles()

    @app.post("/api/positions", status_code=201)
    def new_profile(body: NewProfile) -> dict[str, Any]:
        return rig.create_profile(body.name, body.vehicle, body.zero_reference).as_dict()

    @app.get("/api/positions/{profile}")
    def get_profile(profile: str) -> dict[str, Any]:
        return rig.get_profile(profile).as_dict()

    @app.delete("/api/positions/{profile}")
    def delete_profile(profile: str) -> dict[str, Any]:
        return {"moved_to": rig.delete_profile(profile)}

    @app.post("/api/positions/{profile}/save")
    def save_position(profile: str, body: SavePosition) -> dict[str, Any]:
        return motion(rig.save_position(profile, body.name), body.wait)

    @app.post("/api/positions/{profile}/order")
    def reorder(profile: str, body: Order) -> dict[str, Any]:
        return rig.reorder_positions(profile, body.names).as_dict()

    @app.post("/api/positions/{profile}/gaze")
    def gaze(profile: str) -> dict[str, Any]:
        return rig.set_forward_gaze(profile).as_dict()

    @app.post("/api/positions/{profile}/run")
    def run(profile: str, body: Run | None = None) -> dict[str, Any]:
        body = body or Run()
        return motion(rig.run_profile(profile, body.dwell_s, body.speed), body.wait)

    @app.delete("/api/positions/{profile}/{position}")
    def delete_position(profile: str, position: str) -> dict[str, Any]:
        return rig.delete_position(profile, position).as_dict()

    @app.post("/api/positions/{profile}/{position}/goto")
    def goto_position(profile: str, position: str, body: Wait | None = None) -> dict[str, Any]:
        body = body or Wait()
        return motion(rig.goto_position(profile, position, body.speed), body.wait)

    return app
