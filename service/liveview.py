"""Live view: one camera, any number of viewers.

The camera can only serve one request at a time, so a single grabber thread pulls preview
frames while anyone is watching and keeps the latest; every browser stream sends that frame.
With nobody watching the grabber sleeps, and after `idle_stop_s` it takes the camera out of
live view: on the 2000D live view holds the mirror up and warms the sensor, and the extra dark
current shows in the long bracket frames.

Two ways to watch. A stream (`subscribe()`, the MJPEG route) pushes every frame, and on a
slow link the frames queue in the socket buffers: on the Pi's WiFi that grew to ~10 s behind
the head. The pages pull instead (`next_frame()`): one frame per request, the next asked for
only when the last has arrived, so at most one frame is ever on its way and the view is as
old as one transfer. A pull holds a lease of `lease_s`, renewed by every request, that keeps
the grabber running between requests.

`paused()` takes the camera away from live view for a capture: the grabber stops, live view is
ended, and the caller gets the camera to itself until the block exits.

Camera errors do not stop the server. The state goes to "error" with the message (dial not on
M, cable out, asleep), the camera is closed, and the grabber retries every `retry_s` while
anyone is still watching.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from typing import Any, Iterator

log = logging.getLogger(__name__)


class LiveView:
    def __init__(self, camera: Any | None, source: str = "none", max_fps: float = 10.0,
                 idle_stop_s: float = 5.0, retry_s: float = 2.0, lease_s: float = 3.0) -> None:
        self.camera = camera
        self.source = source if camera is not None else "none"
        self.max_fps = max_fps
        self.idle_stop_s = idle_stop_s
        self.retry_s = retry_s
        self.lease_s = lease_s
        self._cond = threading.Condition()
        self._cam_lock = threading.Lock()     # held for every camera call
        self._viewers = 0
        self._lease_until = 0.0                 # a page pulling frames counts until then
        self._paused = 0
        self._closing = False
        self._connected = False
        self._previewing = False
        self._frame: bytes | None = None
        self._seq = 0
        self._frame_at = 0.0
        self._error: str | None = None
        self._fps = 0.0
        self._idle_since = time.monotonic()
        self._thread: threading.Thread | None = None
        if camera is not None:
            self._thread = threading.Thread(target=self._run, name="liveview", daemon=True)
            self._thread.start()

    # -- viewers ------------------------------------------------------------ #

    def subscribe(self) -> None:
        with self._cond:
            self._viewers += 1
            self._cond.notify_all()

    def unsubscribe(self) -> None:
        with self._cond:
            self._viewers = max(0, self._viewers - 1)
            if self._viewers == 0:
                self._idle_since = time.monotonic()
            self._cond.notify_all()

    def _watched(self) -> bool:
        """Caller holds _cond."""
        return self._viewers > 0 or time.monotonic() < self._lease_until

    def next_frame(self, after: int, timeout: float) -> tuple[int, bytes | None, float | None]:
        """For a pulling viewer: the first frame newer than `after` (the latest on timeout),
        its sequence number and its age in seconds. Renews the lease."""
        with self._cond:
            self._lease_until = max(self._lease_until, time.monotonic() + self.lease_s)
            self._cond.notify_all()
        seq, frame = self.wait_frame(max(after, 0), timeout)   # seq 0: no frame yet
        with self._cond:
            age = time.monotonic() - self._frame_at if frame is not None else None
        return seq, frame, age

    def latest(self) -> tuple[int, bytes | None]:
        with self._cond:
            return self._seq, self._frame

    def wait_frame(self, after: int, timeout: float) -> tuple[int, bytes | None]:
        """The first frame newer than sequence number `after`, or the latest on timeout."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._seq <= after and not self._closing:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._cond.wait(left)
            return self._seq, self._frame

    def snapshot(self, timeout: float = 5.0) -> bytes | None:
        """A frame begun after this call, even when nobody is streaming. None at once without
        a camera. The first new frame is thrown away: the grabber may have started it before
        the call, i.e. while the head was still moving, and a reference image taken from it
        would sit off the position (the aim check and the masks are drawn against it)."""
        if self.camera is None:
            return None
        self.subscribe()
        try:
            deadline = time.monotonic() + timeout
            seq, _ = self.latest()
            seq, frame = self.wait_frame(seq, max(0.0, deadline - time.monotonic()))
            return self.wait_frame(seq, max(0.0, deadline - time.monotonic()))[1] or frame
        finally:
            self.unsubscribe()

    # -- capture hand-over ---------------------------------------------------- #

    @contextlib.contextmanager
    def paused(self) -> Iterator[Any]:
        """Stop live view and hand the camera to the caller for the duration."""
        with self._cond:
            self._paused += 1
            self._cond.notify_all()
        try:
            with self._cam_lock:
                self._end_preview()
                if self.camera is not None and not self._connected:
                    self.camera.connect()   # nobody was watching: it may never have connected
                    self._connected = True
                yield self.camera
        finally:
            with self._cond:
                self._paused -= 1
                self._cond.notify_all()

    # -- status ------------------------------------------------------------- #

    def status(self) -> dict[str, Any]:
        with self._cond:
            if self.camera is None:
                state = "off"
            elif self._paused:
                state = "paused"
            elif self._error:
                state = "error"
            elif self._watched() and self._frame is not None:
                state = "running"
            elif self._watched():
                state = "starting"
            else:
                state = "idle"
            age = time.monotonic() - self._frame_at if self._frame is not None else None
            exposure = getattr(self.camera, "live_exposure", None)
            leased = time.monotonic() < self._lease_until
            return {"source": self.source, "state": state,
                    "viewers": self._viewers + int(leased),
                    "fps": round(self._fps, 1), "error": self._error,
                    "frame_age_s": None if age is None else round(age, 2),
                    "exposure": exposure() if exposure else None}

    def close(self) -> None:
        with self._cond:
            self._closing = True
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(3.0)
        with self._cam_lock:
            self._end_preview()
            if self.camera is not None and self._connected:
                with contextlib.suppress(Exception):
                    self.camera.close()
            self._connected = False

    # -- grabber ------------------------------------------------------------ #

    def _run(self) -> None:
        period = 1.0 / self.max_fps
        last = 0.0
        while True:
            with self._cond:
                while not self._closing and (self._paused or not self._watched()):
                    idle_since = max(self._idle_since, self._lease_until)
                    if (self._previewing and not self._paused
                            and time.monotonic() - idle_since > self.idle_stop_s):
                        break  # leave live view below, outside the condition
                    self._cond.wait(0.5)
                if self._closing:
                    return
                idle = not self._watched() or bool(self._paused)
            if idle:
                with self._cam_lock:
                    self._end_preview()
                continue

            wait = last + period - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            t0 = time.monotonic()
            try:
                with self._cam_lock:
                    if self._paused:
                        continue
                    if not self._connected:
                        self.camera.connect()
                        self._connected = True
                    frame = self.camera.preview()
                    self._previewing = True
            except Exception as exc:  # noqa: BLE001 - any camera failure is reported, not fatal
                self._fail(exc)
                continue
            dt = t0 - last if last else period
            last = t0
            with self._cond:
                self._frame = frame
                self._seq += 1
                self._frame_at = time.monotonic()
                self._error = None
                self._fps = 0.8 * self._fps + 0.2 * (1.0 / max(dt, 1e-3))
                self._cond.notify_all()

    def _fail(self, exc: Exception) -> None:
        msg = f"{type(exc).__name__}: {exc}"
        if msg != self._error:
            log.warning("live view: %s", msg)
        with self._cond:
            self._error = msg
        with self._cam_lock:
            with contextlib.suppress(Exception):
                self.camera.close()
            self._connected = False
            self._previewing = False
        with self._cond:
            self._cond.wait_for(lambda: self._closing, timeout=self.retry_s)

    def _end_preview(self) -> None:
        """Caller holds _cam_lock."""
        if not self._previewing or self.camera is None:
            return
        self._previewing = False
        try:
            self.camera.end_preview()
        except Exception as exc:  # noqa: BLE001
            log.warning("live view: could not end preview: %s", exc)
