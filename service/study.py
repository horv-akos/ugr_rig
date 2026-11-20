"""Recording and analysing a study: the session sequence of REQUIREMENTS §5.4.

    checks -> zero -> calibration shot -> hemisphere sweep -> positions -> check position
                                                                 \\-> analysis, as brackets land

Recording runs as one motion of the rig controller, so nothing else can move the head or take
the camera meanwhile, and the usual stop ramps it down. Live view stays off for the whole
recording: it warms the sensor, and the extra dark current shows in the long frames.

Analysis runs beside it: every finished bracket goes on a queue, and one thread hands them in
order to a worker process (`pipeline.study`). The sweep is recorded first, so its L_b is known
before the first position is analysed. Once recording ends the head is free again, while
analysis finishes on its own.

Steps that are not built yet are recorded as skipped and flagged rather than faked: optical
zeroing (the zero is the one set by hand) and the calibration shot (k comes from
config/analysis.toml, or from the simulator). Without a k the brackets are still recorded and
the analysis waits; a study can be re-analysed from its own folder later.
"""

from __future__ import annotations

import json
import logging
import queue
import shutil
import threading
import time
from typing import TYPE_CHECKING, Any

from pipeline import study as worker

from .positions import PositionProfile, now_iso, slug

if TYPE_CHECKING:
    from .camera_profiles import CameraProfile
    from .controller import RigController

CALIBRATION_FRESH_H = 12   # a calibration this recent counts as the session's calibration shot

log = logging.getLogger(__name__)

STEPS = (("checks", "Checks"), ("zero", "Zero"), ("calibration", "Calibration shot"),
         ("sweep", "Hemisphere sweep"), ("positions", "Positions"),
         ("check", "Check position"), ("analysis", "Analysis"))
LIGHTING = ("dark", "daylight")


def sweep_directions(fov_deg: tuple[float, float], limits: dict[str, list[float]],
                     centre: tuple[float, float], overlap: float = 0.1) -> list[tuple[float, float]]:
    """A 3 x 3 grid around `centre` (the forward gaze, or the zero) that covers most of the
    hemisphere in front of it with this lens: columns ~57 deg apart, rows ~40 deg, clamped to
    the soft limits. What it misses (straight up and down) the sweep reports as coverage."""
    step_p, step_t = fov_deg[0] * (1 - overlap), fov_deg[1] * (1 - overlap)
    (pmin, pmax), (tmin, tmax) = limits["pan"], limits["tilt"]
    out: list[tuple[float, float]] = []
    for dt in (0, 1, -1):
        for dp in (0, -1, 1):
            p = round(min(pmax, max(pmin, centre[0] + dp * step_p)), 3)
            t = round(min(tmax, max(tmin, centre[1] + dt * step_t)), 3)
            if (p, t) not in out:
                out.append((p, t))
    return out


def new_record(study_id: str, profile: PositionProfile, lighting: str, operator: str,
               notes: str, sweep: bool, check: bool, software: dict[str, Any],
               camera: "CameraProfile | None" = None) -> dict[str, Any]:
    return {
        "id": study_id,
        "schema": 1,
        "status": "recording",
        "started_at": now_iso(),
        "finished_at": None,
        "profile": {"name": profile.name, "vehicle": profile.vehicle,
                    "zero_reference": profile.zero_reference,
                    "forward_gaze": list(profile.forward_gaze) if profile.forward_gaze else None,
                    "content_hash": profile.content_hash},
        "camera_profile": None if camera is None else {
            "name": camera.name, "content_hash": camera.content_hash, "k": camera.k,
            "k_measured_at": camera.k_measured_at, "k_source": camera.k_source,
            "k_note": camera.k_note, "lens_name": camera.lens_name,
            "focal_mm": camera.focal_mm, "aperture": camera.aperture, "iso": camera.iso,
            "shutter": list(camera.shutter)},
        "lighting": lighting,
        "operator": operator.strip(),
        "notes": notes.strip(),
        "options": {"sweep": sweep, "check": check},
        "software": software,
        "steps": [{"key": k, "label": label, "state": "pending", "detail": ""}
                  for k, label in STEPS],
        "sweep": {"frames": [], "result": None},
        "positions": [{"id": p.id, "name": p.name, "pan_deg": p.pan_deg, "tilt_deg": p.tilt_deg,
                       "state": "pending", "bracket": None, "masks": None, "result": None,
                       "error": None} for p in profile.positions],
        "check": None,
        "processing": {},
        "summary": {},
        "flags": [],
    }


class StudyRun:
    def __init__(self, rig: "RigController", record: dict[str, Any]) -> None:
        self.rig = rig
        self.study = record
        self.dir = rig.studies.path(record["id"])
        self._lock = threading.RLock()
        self._queue: queue.Queue = queue.Queue()
        self.analysing = threading.Event()
        self.analysing.set()

    # -- record --------------------------------------------------------------- #

    def save(self) -> None:
        with self._lock:
            self.study["summary"] = worker.summarise(self.study)
            self.rig.studies.save(self.study)
            self.rig.index.upsert(self.study)

    def step(self, key: str, state: str, detail: str = "") -> None:
        with self._lock:
            for s in self.study["steps"]:
                if s["key"] == key:
                    s["state"], s["detail"] = state, detail
        self.save()

    def flag(self, text: str) -> None:
        with self._lock:
            if text not in self.study["flags"]:
                self.study["flags"].append(text)

    def prepare(self, profile: PositionProfile, camera: "CameraProfile | None" = None) -> None:
        """Folder, and frozen copies of the profile, the camera profile and each position's
        masks."""
        self.rig.studies.create(self.study["id"])
        (self.dir / "profile.toml").write_text(profile.to_toml(), encoding="utf-8")
        if camera is not None:
            (self.dir / "camera_profile.toml").write_text(camera.to_toml(), encoding="utf-8")
        for pos, entry in zip(profile.positions, self.study["positions"]):
            src = self.rig.profiles.masks_path(profile.name, pos)
            if src.is_file():
                (self.dir / "masks").mkdir(exist_ok=True)
                shutil.copyfile(src, self.dir / "masks" / f"{pos.id}.json")
                entry["masks"] = f"masks/{pos.id}.json"
        self.save()

    # -- recording (the controller's motion) ---------------------------------- #

    def capture(self, stop: threading.Event) -> dict[str, Any]:
        st = self.study
        try:
            with self.rig.live.paused() as cam:   # live view off for the whole recording
                self.step("checks", "done", f"camera {self.rig.live.source}, head zeroed")
                self.step("zero", "skipped", "zero set by hand; optical zeroing is not built")
                self.flag("zero set by hand: no optical zero check, so no drift figure")
                self._camera_setup(cam)

                if st["options"]["sweep"]:
                    self._sweep(cam, stop)
                else:
                    self.step("sweep", "skipped", "L_b from each frame (provisional)")
                    self.flag("no hemisphere sweep: L_b is each frame's mean, provisional")

                if not stop.is_set():
                    self._positions(cam, stop)
                if not stop.is_set() and st["options"]["check"] and st["positions"]:
                    self._check(cam, stop)
                elif not st["options"]["check"]:
                    self.step("check", "skipped", "not requested")
        except Exception as exc:
            with self._lock:
                st["status"] = "failed"
                st["error"] = f"{type(exc).__name__}: {exc}"
                for s in st["steps"]:
                    if s["state"] == "running":
                        s["state"], s["detail"] = "failed", st["error"]
            self.save()
            raise
        finally:
            self._queue.put(None)   # the analysis thread finishes what it has
        if stop.is_set():
            with self._lock:
                st["status"] = "stopped"
                for s in st["steps"]:
                    if s["state"] in ("running", "pending") and s["key"] != "analysis":
                        s["state"], s["detail"] = "stopped", "stopped by the operator"
            self.flag("recording stopped before the end")
        elif st["status"] == "recording":
            st["status"] = "analysing"
        self.save()
        return {"study": st["id"], "status": st["status"]}

    def _camera_setup(self, cam) -> None:
        """Put the camera profile's exposure on the body, check the body and lens against
        it, and decide the calibration step: a calibration of this profile within the last
        CALIBRATION_FRESH_H hours is this session's calibration shot."""
        from hal.camera_config import load_camera_config

        name = (self.study.get("camera_profile") or {}).get("name")
        prof = self.rig.camera_profile(name)
        cam.apply_config(prof.camera_config(load_camera_config()))
        for m in prof.body_check(cam.info()):
            self.flag(m)
        last = prof.k_history[-1] if prof.k_history else None
        age_h = _age_hours(last.at) if last else None
        if prof.k_source == "typed":
            # The newest k was typed: it overrides any measurement before it, and nothing
            # in this session checked it.
            self.step("calibration", "skipped",
                      f"k {prof.k:.5g} typed by hand at {last.at}: {last.note}")
            self.flag(f"k typed by hand, not measured this session ({last.note})")
        elif prof.k_source == "iso":
            # An estimate from the ISO rating checks nothing: never the session's calibration.
            self.step("calibration", "skipped",
                      f"k {prof.k:.5g} estimated from the ISO rating at {last.at}, no meter")
            self.flag("k estimated from the camera's ISO rating (about +-30 %), not measured "
                      "with a meter")
        elif last and age_h is not None and age_h <= CALIBRATION_FRESH_H:
            drift = "" if last.drift is None else f", {100 * last.drift:+.1f} % from the value before"
            self.step("calibration", "done",
                      f"k {last.k:.5g} measured {age_h:.1f} h ago on the target{drift}")
            if last.drift is not None and abs(last.drift) > 0.10:
                self.flag(f"k moved {100 * last.drift:+.1f} % at this session's calibration: "
                          "lens, aperture, filter or in-camera processing changed?")
        elif prof.k:
            self.step("calibration", "skipped",
                      f"no calibration in the last {CALIBRATION_FRESH_H} h; k from "
                      f"{prof.k_measured_at or 'the profile'}")
            self.flag("no calibration shot this session: k is the stored value")
        else:
            self.step("calibration", "skipped",
                      "profile never calibrated" + ("; the simulator's k is used"
                                                    if self.rig.live.source == "sim" else ""))
            if self.rig.live.source != "sim":
                self.flag("camera profile never calibrated: no k")

    def _goto(self, pan: float, tilt: float, stop: threading.Event) -> bool:
        r = self.rig.mount.move_to(pan, tilt, speed=self.rig.speed, stop=stop)
        return not r.stopped

    def _shoot(self, cam, rel: str, meta: dict[str, Any]) -> None:
        pan, tilt = self.rig.mount.position()
        cam.capture_bracket(self.dir / rel, meta={**meta, "pan_deg": round(pan, 4),
                                                  "tilt_deg": round(tilt, 4),
                                                  "study": self.study["id"]})

    def _sweep(self, cam, stop: threading.Event) -> None:
        g = self.study["profile"]["forward_gaze"] or [0.0, 0.0]
        dirs = sweep_directions(tuple(self.rig.geometry["fov_deg"]),
                                self.rig.status()["limits"], (g[0], g[1]))
        frames = self.study["sweep"]["frames"]
        for i, (p, t) in enumerate(dirs):
            if stop.is_set():
                return
            self.step("sweep", "running", f"frame {i + 1} of {len(dirs)}")
            if not self._goto(p, t, stop):
                return
            rel = f"brackets/sweep_{i:02d}"
            self._shoot(cam, rel, {"region": f"sweep_{i:02d}"})
            frames.append({"pan_deg": p, "tilt_deg": t, "bracket": rel})
        self.step("sweep", "done", f"{len(frames)} frames around "
                  f"{'the forward gaze' if self.study['profile']['forward_gaze'] else 'the zero'}")
        self._queue.put(("sweep", None))

    def _positions(self, cam, stop: threading.Event) -> None:
        todo = self.study["positions"]
        for i, pos in enumerate(todo):
            if stop.is_set():
                return
            self.step("positions", "running", f"{i + 1} of {len(todo)}: {pos['name']}")
            pos["state"] = "moving"
            if not self._goto(pos["pan_deg"], pos["tilt_deg"], stop):
                pos["state"] = "pending"
                return
            pos["state"] = "capturing"
            self.save()
            rel = f"brackets/{i + 1:02d}_{pos['id']}"
            self._shoot(cam, rel, {"region": pos["name"], "position_id": pos["id"]})
            pos["bracket"], pos["state"] = rel, "captured"
            self.save()
            self._queue.put(("position", pos))
        self.step("positions", "done", f"{len(todo)} captured")

    def _check(self, cam, stop: threading.Event) -> None:
        """Back to the first position, shot again: the difference in UGR is this session's
        measured repeatability (aim, settle, merge, all of it)."""
        first = self.study["positions"][0]
        self.step("check", "running", f"back to {first['name']}")
        if not self._goto(first["pan_deg"], first["tilt_deg"], stop):
            return
        rel = f"brackets/check_{first['id']}"
        self._shoot(cam, rel, {"region": f"check: {first['name']}", "position_id": first["id"]})
        self.study["check"] = {"id": first["id"], "name": f"check: {first['name']}",
                               "pan_deg": first["pan_deg"], "tilt_deg": first["tilt_deg"],
                               "state": "captured", "bracket": rel, "masks": first["masks"],
                               "result": None, "error": None}
        self.step("check", "done", f"{first['name']} shot again")
        self._queue.put(("position", self.study["check"]))

    # -- analysis (its own thread) -------------------------------------------- #

    def analyse(self) -> None:
        st = self.study
        lb: dict[str, Any] | None = None
        name = (st.get("camera_profile") or {}).get("name")
        k, overrides, notes = self.rig.processing_setup(
            name if name in self.rig.camera_profiles.names() else None)
        with self._lock:
            st["processing"] = {"k": k, "overrides": overrides, "notes": notes}
        if not k:
            self.step("analysis", "pending",
                      "no calibration constant k: brackets are recorded, analysis waits")
            self.flag("not analysed: no k (calibrate, then re-analyse)")
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    break
                if not k:
                    continue
                kind, entry = item
                if kind == "sweep":
                    lb = self._run_sweep(k, overrides)
                else:
                    self._run_position(entry, k, overrides, lb)
            if k:
                n = sum(p.get("result") is not None for p in st["positions"])
                failed = sum(p["state"] == "failed" for p in st["positions"])
                self._write_report()
                self.step("analysis", "failed" if failed and not n else "done",
                          f"{n} position(s) rated" + (f", {failed} failed" if failed else ""))
        finally:
            with self._lock:
                if st["status"] == "analysing":
                    st["status"] = "done"
                elif st["status"] == "recording":   # capture died without a status
                    st["status"] = "failed"
                st["finished_at"] = now_iso()
            self.save()
            self.analysing.clear()

    def _write_report(self) -> None:
        """The Excel workbook, from the folder as it now stands."""
        self.step("analysis", "running", "Excel workbook")
        with self._lock:
            self.study["summary"] = worker.summarise(self.study)
            snapshot = json.loads(json.dumps(self.study, default=str))
        # Written as the last step of the analysis: give it the status the study ends with.
        if snapshot["status"] == "analysing":
            snapshot["status"] = "done"
        snapshot["finished_at"] = snapshot.get("finished_at") or now_iso()
        try:
            self.rig.pool().submit(worker.write_report,
                                   {"dir": str(self.dir), "study": snapshot}).result()
        except Exception as exc:   # noqa: BLE001 - the results stand without the workbook
            log.warning("Excel report failed: %s", exc)
            self.flag(f"Excel workbook could not be written ({exc})")
            return
        with self._lock:
            self.study.setdefault("outputs", {})["excel"] = "report.xlsx"

    # -- re-analysis ------------------------------------------------------------ #

    def prepare_reanalysis(self) -> None:
        """Move the previous analysis aside (kept under previous/<time>/), reset every
        result, and queue the recorded brackets again: sweep first, then positions, check.
        The study's frozen masks are used, not the profile's current ones."""
        st = self.study
        stamp = now_iso().replace(":", "").replace("-", "")
        old = self.dir / "previous" / stamp
        moved = []
        for name in ("results", "background.json", "report.xlsx"):
            src = self.dir / name
            if src.exists():
                old.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(old / name))
                moved.append(name)
        with self._lock:
            st.setdefault("analyses", []).append(
                {"at": now_iso(), "previous": f"previous/{stamp}" if moved else None,
                 "processing": st.get("processing"), "software": st.get("software"),
                 "summary": st.get("summary")})
            st["software"] = self.rig.software
            st["status"] = "analysing"
            st["finished_at"] = None
            st["sweep"]["result"] = None
            st.pop("outputs", None)
            st["flags"] = [f for f in st["flags"] if not f.startswith((
                "not analysed: no k", "hemisphere sweep could", "sweep covered",
                "Excel workbook"))]
            for e in st["positions"] + ([st["check"]] if st.get("check") else []):
                e["result"], e["error"] = None, None
                e["state"] = "captured" if e.get("bracket") else "pending"
            for s in st["steps"]:
                if s["key"] == "analysis":
                    s["state"], s["detail"] = "pending", "re-analysing the recorded brackets"
        if st["sweep"]["frames"]:
            self._queue.put(("sweep", None))
        for e in st["positions"] + ([st["check"]] if st.get("check") else []):
            if e.get("bracket"):
                self._queue.put(("position", e))
        self._queue.put(None)
        self.save()

    def _run_sweep(self, k: float, overrides: dict[str, Any]) -> dict[str, Any] | None:
        self.step("analysis", "running", "hemisphere sweep -> L_b")
        g = self.study["profile"]["forward_gaze"]
        job = {"brackets": [str(self.dir / f["bracket"]) for f in self.study["sweep"]["frames"]],
               "out": str(self.dir / "background.json"), "k": k,
               "overrides": {**overrides, **({"ugr.line_of_sight": g} if g else {})}}
        try:
            res = self.rig.pool().submit(worker.process_sweep, job).result()
        except Exception as exc:   # noqa: BLE001 - flagged, positions fall back to frame L_b
            log.warning("sweep analysis failed: %s", exc)
            self.flag(f"hemisphere sweep could not be analysed ({exc}): L_b provisional")
            return None
        with self._lock:
            self.study["sweep"]["result"] = res
        if res.get("provisional"):
            self.flag(f"sweep covered {100 * res['coverage_cos_weighted']:.0f} % of the "
                      "hemisphere (cosine-weighted): L_b provisional")
        self.save()
        return res

    def _run_position(self, entry: dict[str, Any], k: float, overrides: dict[str, Any],
                      lb: dict[str, Any] | None) -> None:
        entry["state"] = "analysing"
        self.step("analysis", "running", entry["name"])
        rel = "results/" + entry["bracket"].split("/", 1)[1]
        job = {"bracket": str(self.dir / entry["bracket"]), "out": str(self.dir / rel),
               "masks": str(self.dir / entry["masks"]) if entry.get("masks") else None,
               "k": k, "lb": lb, "overrides": overrides, "name": entry["name"],
               "study": self.study["id"]}
        t0 = time.monotonic()
        try:
            res = self.rig.pool().submit(worker.process_region, job).result()
        except Exception as exc:   # noqa: BLE001 - this position fails, the study goes on
            log.warning("analysis of %s failed: %s", entry["name"], exc)
            entry["state"], entry["error"] = "failed", f"{type(exc).__name__}: {exc}"
            self.save()
            return
        res["dir"] = rel
        res["outputs"] = {key: f"{rel}/{name}" for key, name in res["outputs"].items()}
        res["seconds"] = round(time.monotonic() - t0, 2)
        entry["result"], entry["state"] = res, "done"
        self.save()


def _age_hours(iso: str) -> float | None:
    from datetime import datetime, timezone

    try:
        t = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - t).total_seconds() / 3600


def study_id_for(rig: "RigController", profile_name: str) -> str:
    return rig.studies.new_id(slug(profile_name))
