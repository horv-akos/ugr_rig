#!/usr/bin/env python3
"""Command-line client of the rig server.

The server (`python -m web`) owns the head. This is only a client of its HTTP API, because
the head is open-loop: its position lives in the server's memory, and a CLI that drove the
mount itself would lose it on exit. Start the server first:

    python -m web                       # simulated head on http://127.0.0.1:8000

Then, from the repository root:

    python -m tools.rigctl status
    python -m tools.rigctl mount enable
    python -m tools.rigctl mount zero                 # "save home", backlash taken up first
    python -m tools.rigctl mount left 2               # nudge 2 deg; right / up / down
    python -m tools.rigctl mount goto 30 -10
    python -m tools.rigctl mount speed 0.5            # fraction of the configured maximum
    python -m tools.rigctl mount jog                  # interactive: arrow keys
    python -m tools.rigctl positions new truck-a --vehicle "Truck A"
    python -m tools.rigctl positions save truck-a dash
    python -m tools.rigctl positions run truck-a
    python -m tools.rigctl selftest                   # own sim server, known answers

`--url` (or RIG_URL) points at another server, e.g. http://10.42.0.1:8000 on the Pi.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEFAULT_URL = os.environ.get("RIG_URL", "http://127.0.0.1:8000")
PING_S = 0.1                     # well inside the server's 0.3 s heartbeat timeout
NUDGE_STEPS = (0.1, 1.0, 5.0)


def _quote(path: str) -> str:
    """Quote a path (profile names have spaces), but leave a query string as built:
    its values are quoted by the caller."""
    base, sep, query = path.partition("?")
    return urllib.parse.quote(base) + sep + query


class RigCtlError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# client
# --------------------------------------------------------------------------- #


class Client:
    def __init__(self, url: str = DEFAULT_URL, timeout: float = 120.0) -> None:
        self.url = url.rstrip("/")
        self.timeout = timeout

    def call(self, method: str, path: str, body: dict | None = None) -> Any:
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.url + _quote(path), data=data, method=method,
            headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as exc:
            try:
                err = json.loads(exc.read())
            except ValueError:
                raise RigCtlError(f"HTTP {exc.code} from {path}") from None
            if isinstance(err.get("detail"), list):  # request validation
                msgs = "; ".join(f"{'.'.join(map(str, e['loc'][1:]))}: {e['msg']}"
                                 for e in err["detail"])
                raise RigCtlError(f"bad request: {msgs}") from None
            raise RigCtlError(f"{err.get('error', exc.code)}: {err.get('detail')}") from None
        except urllib.error.URLError as exc:
            raise RigCtlError(f"no rig server at {self.url} ({exc.reason}) - "
                              "start it with `python -m web`") from None

    def get(self, path: str) -> Any:
        return self.call("GET", path)

    def post(self, path: str, body: dict | None = None) -> Any:
        return self.call("POST", path, body or {})

    def delete(self, path: str) -> Any:
        return self.call("DELETE", path)

    def download(self, path: str, dest: Path) -> int:
        """Stream a file (a raw zip can be GBs) to `dest`; returns the bytes written."""
        req = urllib.request.Request(self.url + _quote(path))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp, \
                    open(dest, "wb") as out:
                n = 0
                while block := resp.read(1 << 20):
                    out.write(block)
                    n += len(block)
                return n
        except urllib.error.HTTPError as exc:
            raise RigCtlError(f"HTTP {exc.code} from {path}") from None
        except urllib.error.URLError as exc:
            raise RigCtlError(f"no rig server at {self.url} ({exc.reason})") from None


def motion_line(m: dict | None) -> str:
    if not m:
        return "nothing was moving"
    if m.get("error"):
        return f"{m['kind']} failed: {m['error']['type']}: {m['error']['detail']}"
    r = m.get("result") or {}
    parts = [m["kind"]]
    if "pan_deg" in r:
        parts.append(f"at {r['pan_deg']:+.3f}, {r['tilt_deg']:+.3f} deg")
    if "visited" in r:
        parts.append(f"{len(r['visited'])} position(s), "
                     + ("complete" if r.get("complete") else "incomplete"))
    if r.get("stopped"):
        parts.append(f"stopped ({m.get('stop_reason') or 'stop'})")
    if r.get("backlash_stage") or r.get("backlash_taken_up"):
        parts.append("backlash taken up")
    if m.get("running"):
        parts.append("running")
    return ", ".join(parts)


def status_line(st: dict) -> str:
    flags = []
    if not st["enabled"]:
        flags.append("drivers OFF")
    if not st["zeroed"]:
        flags.append("NO ZERO")
    elif not st["certain"]:
        flags.append("position UNCERTAIN - re-zero")
    lash = st.get("backlash_ok", {})
    if st["zeroed"] and not all(lash.values()):
        flags.append("backlash not taken up: " + ", ".join(a for a, ok in lash.items() if not ok))
    return (f"pan {st['pan_deg']:+8.3f}  tilt {st['tilt_deg']:+8.3f} deg   "
            f"{st['state']:<6}  speed {st['speed']:.2f}"
            + ("   " + "; ".join(flags) if flags else ""))


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


def cmd_status(c: Client, args) -> int:
    st = c.get("/api/status")
    if args.json:
        print(json.dumps(st, indent=2))
        return 0
    print(status_line(st))
    if st.get("last_motion"):
        print(f"last: {motion_line(st['last_motion'])}")
    if st.get("last_known") and not st["zeroed"]:
        lk = st["last_known"]
        print(f"before restart: pan {lk.get('pan_deg')}, tilt {lk.get('tilt_deg')} "
              f"(hint only, re-zero)")
    return 0


def cmd_simple(c: Client, args) -> int:
    path = {"enable": "/api/mount/enable", "disable": "/api/mount/disable",
            "stop": "/api/mount/stop", "estop": "/api/mount/estop",
            "zero": "/api/mount/zero", "backlash": "/api/mount/backlash"}[args.action]
    out = c.post(path, {"wait": True} if args.action in ("zero", "backlash") else None)
    if "status" in out:  # a motion reply; enable/disable answer with the status itself
        print(motion_line(out["motion"]))
        out = out["status"]
    print(status_line(out))
    return 0


def cmd_nudge(c: Client, args) -> int:
    out = c.post("/api/mount/nudge", {"direction": args.direction, "deg": args.deg,
                                      "speed": args.speed, "wait": True})
    print(motion_line(out["motion"]))
    return 0


def cmd_goto(c: Client, args) -> int:
    out = c.post("/api/mount/goto", {"pan": args.pan, "tilt": args.tilt,
                                     "speed": args.speed, "wait": True})
    print(motion_line(out["motion"]))
    return 0


def cmd_speed(c: Client, args) -> int:
    print(status_line(c.post("/api/mount/speed", {"fraction": args.fraction})))
    return 0


def cmd_jog(c: Client, args) -> int:
    return interactive(c)


def cmd_pos_list(c: Client, args) -> int:
    rows = c.get("/api/positions")
    if not rows:
        print("no position profiles")
    for r in rows:
        print(f"{r['name']:<30} {r['vehicle']:<20} {r['positions']:>3} positions  {r['updated_at']}")
    return 0


def cmd_pos_show(c: Client, args) -> int:
    p = c.get(f"/api/positions/{args.profile}")
    print(f"{p['name']}   vehicle: {p['vehicle'] or '-'}")
    print(f"zero reference: {p['zero_reference'] or '-'}")
    gaze = p["forward_gaze"]
    print(f"forward gaze: {'not set' if not gaze else f'{gaze[0]:+.3f}, {gaze[1]:+.3f} deg'}")
    for i, pos in enumerate(p["positions"], 1):
        print(f"  {i:>2}. {pos['name']:<24} pan {pos['pan_deg']:+8.3f}  "
              f"tilt {pos['tilt_deg']:+8.3f}   {pos['saved_at']}")
    return 0


def cmd_pos_new(c: Client, args) -> int:
    p = c.post("/api/positions", {"name": args.profile, "vehicle": args.vehicle,
                                  "zero_reference": args.zero_ref})
    print(f"created {p['name']}")
    return 0


def cmd_pos_copy(c: Client, args) -> int:
    p = c.post("/api/positions", {"name": args.new, "copy_from": args.profile,
                                  "vehicle": args.vehicle or "", "zero_reference": ""})
    print(f"copied {args.profile} to {p['name']}: {len(p['positions'])} positions")
    return 0


# --------------------------------------------------------------------------- #
# studies
# --------------------------------------------------------------------------- #


def _ugr(v) -> str:
    return "-" if v is None else f"{v:.1f}"


def study_lines(st: dict) -> list[str]:
    sm, fw = st.get("summary") or {}, st.get("forward")
    out = [f"{st['id']}  {st['status']}  {st['profile']['name']}  {st['lighting']}  "
           f"started {st['started_at']}"]
    for s in st["steps"]:
        out.append(f"  {s['state']:<8} {s['label']:<18} {s['detail']}")
    if fw:
        out.append(f"forward gaze UGR {'>= ' if fw.get('lower_bound') else ''}{_ugr(fw.get('ugr'))}"
                   f" ({fw.get('category')}), {100 * fw['coverage']:.0f} % covered, "
                   f"{fw['n_sources']} source(s)")
    for p in st["positions"] + ([st["check"]] if st.get("check") else []):
        r = p.get("result")
        out.append(f"  {p['name']:<24} " + (f"UGR {_ugr(r.get('ugr'))} ({r.get('category')}), "
                                             f"{r.get('n_sources')} source(s)" if r
                                             else p.get("error") or p.get("state", "")))
    if sm.get("ugr_max") is not None:
        out.append(f"highest (camera axis) {_ugr(sm['ugr_max'])} at {sm['ugr_max_position']}")
    for f in st.get("flags", []):
        out.append(f"  flag: {f}")
    return out


def _follow(c: Client, sid: str, timeout: float) -> dict:
    """Print each step as it changes until the study has finished."""
    seen: dict[str, str] = {}
    deadline = time.monotonic() + timeout
    while True:
        st = c.get(f"/api/studies/{sid}")
        for s in st["steps"]:
            key = f"{s['state']}|{s['detail']}"
            if seen.get(s["key"]) != key:
                seen[s["key"]] = key
                if s["state"] != "pending":
                    print(f"  {s['state']:<8} {s['label']:<18} {s['detail']}", flush=True)
        if st["status"] in ("done", "failed", "stopped", "interrupted"):
            return st
        if time.monotonic() > deadline:
            raise RigCtlError(f"{sid} still {st['status']} after {timeout:.0f} s")
        time.sleep(0.5)


def cmd_study_start(c: Client, args) -> int:
    body = {"profile": args.profile, "lighting": args.lighting, "operator": args.operator,
            "notes": args.notes, "sweep": not args.no_sweep, "check": not args.no_check}
    if args.camera_profile:
        body["camera_profile"] = args.camera_profile
    sid = c.post("/api/studies", body)["study"]
    print(f"started {sid}")
    if args.wait:
        st = _follow(c, sid, args.timeout)
        print("\n".join(study_lines(st)[len(st["steps"]) + 1:]))
        return 0 if st["status"] == "done" else 1
    return 0


def cmd_study_list(c: Client, args) -> int:
    rows = c.get(f"/api/studies?q={urllib.parse.quote(args.query or '')}")
    if not rows:
        print("no studies")
    for r in rows:
        fw = "-" if r.get("ugr_forward") is None else \
            f"{'>=' if r.get('forward_lower_bound') else ''}{r['ugr_forward']:.1f}"
        print(f"{r['id']:<28} {r['status']:<10} {r['lighting']:<8} forward {fw:>6}  "
              f"max {_ugr(r['ugr_max']):>5}  {r['n_flags']} flag(s)")
    return 0


def cmd_study_show(c: Client, args) -> int:
    st = c.get(f"/api/studies/{args.study}")
    print(json.dumps(st, indent=2) if args.json else "\n".join(study_lines(st)))
    return 0


def cmd_study_wait(c: Client, args) -> int:
    st = _follow(c, args.study, args.timeout)
    print(f"{args.study} {st['status']}")
    return 0 if st["status"] == "done" else 1


def cmd_study_reanalyse(c: Client, args) -> int:
    c.post(f"/api/studies/{args.study}/reanalyse")
    print(f"re-analysing {args.study}")
    if args.wait:
        st = _follow(c, args.study, args.timeout)
        return 0 if st["status"] == "done" else 1
    return 0


def cmd_study_download(c: Client, args) -> int:
    st = c.get(f"/api/studies/{args.study}")
    outs = st.get("outputs") or {}
    paths = {"results": f"/api/studies/{args.study}/results.zip",
             "raw": f"/api/studies/{args.study}/raw.zip",
             "pdf": f"/api/studies/{args.study}/files/{outs['pdf']}" if outs.get("pdf") else None,
             "excel": f"/api/studies/{args.study}/files/{outs['excel']}" if outs.get("excel") else None}
    if not paths[args.what]:
        raise RigCtlError(f"{args.study} has no {args.what} (not analysed yet?)")
    names = {"results": f"{args.study}.zip", "raw": f"{args.study}-raw.zip",
             "pdf": f"{args.study}.pdf", "excel": f"{args.study}.xlsx"}
    dest = Path(args.output or names[args.what])
    n = c.download(paths[args.what], dest)
    print(f"{dest}  {n / 1e6:.1f} MB")
    return 0


def cmd_shutdown(c: Client, args) -> int:
    if not args.yes:
        raise RigCtlError("this powers the Pi off: add --yes")
    c.post("/api/system/shutdown")
    print("the rig is shutting down: switch the supply off once the Pi's green LED is still")
    return 0


def cmd_pos_save(c: Client, args) -> int:
    out = c.post(f"/api/positions/{args.profile}/save", {"name": args.name, "wait": True})
    m = out["motion"]
    pos = (m.get("result") or {}).get("position")
    if pos:
        notes = [n for n, on in (("backlash taken up first", m["result"]["backlash_taken_up"]),
                                 ("reference image saved", m["result"].get("reference_image")))
                 if on]
        print(f"saved {pos['name']}: pan {pos['pan_deg']:+.3f}, tilt {pos['tilt_deg']:+.3f}"
              + (f" ({'; '.join(notes)})" if notes else ""))
    else:
        print(motion_line(m))
    return 0


def cmd_pos_delete(c: Client, args) -> int:
    if args.name:
        c.delete(f"/api/positions/{args.profile}/{args.name}")
        print(f"deleted {args.name} from {args.profile}")
    else:
        out = c.delete(f"/api/positions/{args.profile}")
        print(f"moved {args.profile} to {out['moved_to']}")
    return 0


def cmd_pos_goto(c: Client, args) -> int:
    out = c.post(f"/api/positions/{args.profile}/{args.name}/goto",
                 {"wait": True, "speed": args.speed})
    print(motion_line(out["motion"]))
    return 0


def cmd_pos_order(c: Client, args) -> int:
    p = c.post(f"/api/positions/{args.profile}/order", {"names": args.names})
    print(", ".join(pos["name"] for pos in p["positions"]))
    return 0


def cmd_pos_gaze(c: Client, args) -> int:
    p = c.post(f"/api/positions/{args.profile}/gaze")
    g = p["forward_gaze"]
    print(f"forward gaze of {p['name']}: pan {g[0]:+.3f}, tilt {g[1]:+.3f}")
    return 0


def cmd_pos_run(c: Client, args) -> int:
    out = c.post(f"/api/positions/{args.profile}/run",
                 {"dwell_s": args.dwell, "speed": args.speed, "wait": True})
    m = out["motion"]
    if m.get("error"):
        print(motion_line(m))
        return 1
    for v in m["result"]["visited"]:
        print(f"  {v['name']:<24} pan {v['pan_deg']:+8.3f}  tilt {v['tilt_deg']:+8.3f}"
              + ("   stopped" if v["stopped"] else ""))
    print("complete" if m["result"]["complete"] else f"stopped ({m.get('stop_reason')})")
    return 0


# --------------------------------------------------------------------------- #
# interactive jog
# --------------------------------------------------------------------------- #

HELP = """\
arrows   nudge by the step size (or start a jog in hold mode)
[ ]      smaller / larger step: 0.1, 1, 5 deg
h        toggle hold mode: an arrow jogs until any key is pressed
space    stop        z  set zero (save home)        s  save position
q        quit
"""


class Keys:
    """Single key presses without Enter, on Linux (the Pi) and Windows (the desk)."""

    def __enter__(self) -> "Keys":
        if os.name == "nt":
            import msvcrt
            self._ms = msvcrt
        else:
            import termios
            import tty
            self._fd = sys.stdin.fileno()
            self._old = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        return self

    def __exit__(self, *exc) -> None:
        if os.name != "nt":
            import termios
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)

    def read(self, timeout: float) -> str | None:
        """One key name ('up', 'q', ' ', ...) or None after `timeout` seconds."""
        if os.name == "nt":
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self._ms.kbhit():
                    ch = self._ms.getwch()
                    if ch in ("\x00", "\xe0"):
                        return {"H": "up", "P": "down", "K": "left", "M": "right"}.get(
                            self._ms.getwch())
                    return ch
                time.sleep(0.01)
            return None
        import select
        if not select.select([sys.stdin], [], [], timeout)[0]:
            return None
        ch = os.read(self._fd, 1).decode(errors="replace")
        if ch == "\x1b" and select.select([sys.stdin], [], [], 0.05)[0]:
            seq = os.read(self._fd, 2).decode(errors="replace")
            return {"[A": "up", "[B": "down", "[D": "left", "[C": "right"}.get(seq)
        return ch

    def line(self, prompt: str) -> str:
        """Ordinary line input in the middle of key mode."""
        self.__exit__()
        try:
            return input(prompt)
        finally:
            self.__enter__()


def interactive(c: Client) -> int:
    if not sys.stdin.isatty():
        raise RigCtlError("interactive jog needs a terminal")
    st = c.get("/api/status")
    if not st["enabled"]:
        raise RigCtlError("drivers are off - `rigctl mount enable` first")
    print(HELP)
    step = 1
    hold = False
    jogging = False
    print(status_line(st))
    with Keys() as keys:
        while True:
            key = keys.read(PING_S)
            if jogging:
                if key is None:
                    if not c.post("/api/mount/jog/ping")["jogging"]:
                        jogging = False  # hit a limit or the server stopped it
                        print(status_line(c.get("/api/status")))
                    continue
                c.post("/api/mount/jog/stop")
                jogging = False
                time.sleep(0.05)
                _wait_idle(c)
                print(status_line(c.get("/api/status")))
                if key != "q":
                    continue
            if key is None:
                continue
            try:
                if key == "q":
                    return 0
                if key in ("up", "down", "left", "right"):
                    if hold:
                        c.post("/api/mount/jog/start", {"direction": key})
                        jogging = True
                        continue
                    out = c.post("/api/mount/nudge", {"direction": key,
                                                      "deg": NUDGE_STEPS[step], "wait": True})
                    print(motion_line(out["motion"]))
                elif key in "[]":
                    step = max(0, min(len(NUDGE_STEPS) - 1, step + (1 if key == "]" else -1)))
                    print(f"step {NUDGE_STEPS[step]:g} deg")
                elif key == "h":
                    hold = not hold
                    print("hold mode: arrows jog until any key" if hold else
                          f"nudge mode: arrows step {NUDGE_STEPS[step]:g} deg")
                elif key == " ":
                    print(motion_line(c.post("/api/mount/stop")["motion"]))
                elif key == "z":
                    print(motion_line(c.post("/api/mount/zero", {"wait": True})["motion"]))
                elif key == "s":
                    profile = keys.line("profile: ").strip()
                    name = keys.line("position name: ").strip()
                    if profile and name:
                        out = c.post(f"/api/positions/{profile}/save",
                                     {"name": name, "wait": True})
                        print(motion_line(out["motion"]))
                else:
                    continue
                print(status_line(c.get("/api/status")))
            except RigCtlError as exc:
                print(f"  {exc}")


def _wait_idle(c: Client, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        st = c.get("/api/status")
        if st["state"] == "idle" or time.monotonic() > deadline:
            return st
        time.sleep(0.05)


# --------------------------------------------------------------------------- #
# selftest
# --------------------------------------------------------------------------- #


def cmd_selftest(args) -> int:
    """Start a private server on the simulated head, drive it over HTTP with this
    client, and check the answers. No hardware, no running server needed."""
    import dataclasses
    import logging

    import uvicorn

    from hal.backend import SimBackend
    from hal.config import load_config
    from hal.mount import PanTiltMount
    from service.controller import HEARTBEAT_TIMEOUT_S, RigController
    from web.api import create_app

    logging.getLogger("service").setLevel(logging.ERROR)  # refusals below are intended

    failures: list[str] = []

    def check(label: str, cond: bool, detail: str = "") -> None:
        print(f"  {'ok  ' if cond else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
        if not cond:
            failures.append(label)

    def refused(fn, kind: str) -> tuple[bool, str]:
        try:
            fn()
        except RigCtlError as exc:
            return kind in str(exc), str(exc)
        return False, "accepted"

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    data = tempfile.mkdtemp(prefix="rigctl-selftest-")
    # 10x real time: moves are quick, but the 0.3 s heartbeat still means something. No
    # settle dwell: it is a real sleep, and nothing here captures.
    mount_cfg = dataclasses.replace(load_config(), settle_ms=0)
    rig = RigController(PanTiltMount(mount_cfg, SimBackend(time_scale=0.1)), data)
    server = uvicorn.Server(uvicorn.Config(create_app(rig), host="127.0.0.1", port=port,
                                           log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    t0 = time.monotonic()
    while not server.started and time.monotonic() - t0 < 10:
        time.sleep(0.02)
    c = Client(f"http://127.0.0.1:{port}", timeout=60)
    cfg = rig.mount.cfg

    try:
        print("power and datum")
        ok, why = refused(lambda: c.post("/api/mount/nudge", {"direction": "left"}), "enabled")
        check("refuses to move with drivers off", ok, why)
        c.post("/api/mount/enable")
        ok, why = refused(lambda: c.post("/api/mount/goto", {"pan": 1, "tilt": 0}), "NotZeroed")
        check("refuses goto before a zero", ok, why)
        out = c.post("/api/mount/nudge", {"direction": "right", "deg": 2, "wait": True})
        r = out["motion"]["result"]
        check("nudge works before a zero (to find the fiducial)",
              abs(r["pan_deg"] - 2.0) < cfg.pan.deg_per_step, f"{r['pan_deg']:+.4f}")
        ok, why = refused(lambda: c.post("/api/mount/nudge",
                                         {"direction": "right", "deg": cfg.unzeroed_jog_deg + 1}),
                          "capped")
        check("unzeroed nudge capped", ok, why)
        out = c.post("/api/mount/zero", {"wait": True})
        st = out["status"]
        check("zero set", st["zeroed"] and st["pan_deg"] == 0 and st["tilt_deg"] == 0)
        check("zero leaves backlash taken up", all(st["backlash_ok"].values()))

        print("goto and speed")
        r = c.post("/api/mount/goto", {"pan": 10, "tilt": -5, "wait": True})["motion"]["result"]
        check("arrives", abs(r["pan_deg"] - 10) < cfg.pan.deg_per_step
              and abs(r["tilt_deg"] + 5) < cfg.tilt.deg_per_step,
              f"{r['pan_deg']:+.4f}, {r['tilt_deg']:+.4f}")
        check("tilt arrived from below (two-stage)", r["backlash_stage"])
        # The same pan-only move twice, long enough that cruise outweighs ramps and settle.
        c.post("/api/mount/goto", {"pan": 0, "tilt": -5, "wait": True})
        full = c.post("/api/mount/goto", {"pan": 40, "tilt": -5,
                                          "wait": True})["motion"]["result"]["motion_s"]
        c.post("/api/mount/goto", {"pan": 0, "tilt": -5, "wait": True})
        r = c.post("/api/mount/goto", {"pan": 40, "tilt": -5, "speed": 0.5,
                                       "wait": True})["motion"]["result"]
        check("half speed is slower", r["motion_s"] > full * 1.2,
              f"{r['motion_s']:.2f} s vs {full:.2f} s")
        ok, why = refused(lambda: c.post("/api/mount/goto", {"pan": cfg.pan.max_deg + 5,
                                                             "tilt": 0}), "SoftLimit")
        check("soft limit", ok, why)

        print("jog")
        c.post("/api/mount/jog/start", {"direction": "left"})
        ok, why = refused(lambda: c.post("/api/mount/goto", {"pan": 0, "tilt": 0}), "RigBusy")
        check("busy while jogging", ok, why)
        for _ in range(4):
            time.sleep(PING_S)
            c.post("/api/mount/jog/ping")
        c.post("/api/mount/jog/stop")
        st = _wait_idle(c)
        m = st["last_motion"]
        check("release ramps down", m["result"]["stopped"] and m["stop_reason"] == "released",
              f"at {m['result']['pan_deg']:+.3f}")
        check("position still certain after a stop", st["certain"])
        check("left jog leaves pan backlash to take up", not st["backlash_ok"]["pan"])

        c.post("/api/mount/jog/start", {"direction": "up"})
        t0 = time.monotonic()
        st = _wait_idle(c)
        m = st["last_motion"]
        check("no pings: the dead-man stops the jog", m["stop_reason"] == "heartbeat lost",
              f"stopped by {m['stop_reason']!r} after {time.monotonic() - t0:.2f} s "
              f"(timeout {HEARTBEAT_TIMEOUT_S} s + ramp)")

        print("positions")
        c.post("/api/positions", {"name": "selftest cab", "vehicle": "sim"})
        before = c.get("/api/status")
        out = c.post("/api/positions/selftest cab/save", {"name": "dash", "wait": True})
        res = out["motion"]["result"]
        check("save takes up backlash first", res["backlash_taken_up"]
              and all(out["status"]["backlash_ok"].values()))
        check("saved angle is the commanded one", abs(res["position"]["pan_deg"]
                                                      - before["pan_deg"]) < 1e-3)
        c.post("/api/mount/goto", {"pan": 20, "tilt": 10, "wait": True})
        c.post("/api/positions/selftest cab/save", {"name": "roof", "wait": True})
        r = c.post("/api/positions/selftest cab/dash/goto", {"wait": True})["motion"]["result"]
        check("replay lands on the saved angle",
              abs(r["pan_deg"] - res["position"]["pan_deg"]) < cfg.pan.deg_per_step
              and abs(r["tilt_deg"] - res["position"]["tilt_deg"]) < cfg.tilt.deg_per_step,
              f"{r['pan_deg']:+.4f}, {r['tilt_deg']:+.4f}")
        out = c.post("/api/positions/selftest cab/run", {"wait": True})
        check("run visits every position", out["motion"]["result"]["complete"]
              and [v["name"] for v in out["motion"]["result"]["visited"]] == ["dash", "roof"])
        p = c.post("/api/positions/selftest cab/order", {"names": ["roof", "dash"]})
        check("reorder", [x["name"] for x in p["positions"]] == ["roof", "dash"])
        p = c.post("/api/positions/selftest cab/gaze")
        check("forward gaze recorded", p["forward_gaze"] is not None)
        q = c.post("/api/positions", {"name": "selftest copy", "copy_from": "selftest cab"})
        check("copy keeps positions, ids and the gaze",
              [(x["name"], x["id"]) for x in q["positions"]]
              == [(x["name"], x["id"]) for x in p["positions"]]
              and q["forward_gaze"] == p["forward_gaze"])
        ok, why = refused(lambda: c.post("/api/positions", {"name": "selftest copy",
                                                           "copy_from": "selftest cab"}),
                          "ProfileError")
        check("copy refuses an existing name", ok, why)
        c.delete("/api/positions/selftest copy")
        ok, why = refused(lambda: c.get("/api/positions/no such cab"), "ProfileNotFound")
        check("unknown profile is a 404", ok, why)
        out = c.delete("/api/positions/selftest cab")
        check("delete moves the profile to the trash", ".trash" in out["moved_to"]
              and not c.get("/api/positions"))

        print("stop and emergency stop")
        c.post("/api/mount/goto", {"pan": -60, "tilt": 0})
        time.sleep(0.3)
        c.post("/api/mount/stop")
        st = _wait_idle(c)
        check("stop ends a goto early, position certain",
              st["last_motion"]["result"]["stopped"] and st["certain"]
              and st["pan_deg"] > -60, f"at {st['pan_deg']:+.3f}")
        c.post("/api/mount/goto", {"pan": 60, "tilt": 0})
        time.sleep(0.3)
        c.post("/api/mount/estop")
        st = _wait_idle(c)
        check("emergency stop: position flagged uncertain", not st["certain"]
              and st["last_motion"]["error"]["type"] == "MoveAborted")
        c.post("/api/mount/disable")
        check("disable drops the zero", not c.get("/api/status")["zeroed"])
    except RigCtlError as exc:
        check("unexpected error", False, str(exc))
    finally:
        server.should_exit = True
        thread.join(5)
        rig.close()

    print(f"\n{'all checks passed' if not failures else str(len(failures)) + ' FAILED'}")
    return 1 if failures else 0


# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="rigctl", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=DEFAULT_URL, help="rig server (default: %(default)s)")
    sub = p.add_subparsers(dest="cmd", required=True)

    st = sub.add_parser("status", help="position, state, flags")
    st.add_argument("--json", action="store_true")
    st.set_defaults(fn=cmd_status)
    sub.add_parser("selftest", help="private sim server, known answers").set_defaults(
        fn=None, selftest=True)

    m = sub.add_parser("mount", help="move the head").add_subparsers(dest="mcmd", required=True)
    for action, text in (("enable", "energise the drivers"),
                         ("disable", "release the drivers (drops the zero)"),
                         ("zero", "save home: take up backlash, declare this pose 0, 0"),
                         ("backlash", "take up backlash along the approach direction"),
                         ("stop", "ramp down whatever is moving"),
                         ("estop", "cut the motion at once (position becomes uncertain)")):
        m.add_parser(action, help=text).set_defaults(fn=cmd_simple, action=action)
    for direction in ("left", "right", "up", "down"):
        n = m.add_parser(direction, help=f"nudge {direction}")
        n.add_argument("deg", type=float, nargs="?", default=1.0)
        n.add_argument("--speed", type=float, default=None)
        n.set_defaults(fn=cmd_nudge, direction=direction)
    g = m.add_parser("goto", help="absolute move, degrees from the zero")
    g.add_argument("pan", type=float)
    g.add_argument("tilt", type=float)
    g.add_argument("--speed", type=float, default=None)
    g.set_defaults(fn=cmd_goto)
    s = m.add_parser("speed", help="default speed, fraction of the configured maximum")
    s.add_argument("fraction", type=float)
    s.set_defaults(fn=cmd_speed)
    m.add_parser("jog", help="interactive: arrow keys, hold mode, zero, save").set_defaults(
        fn=cmd_jog)

    pos = sub.add_parser("positions", help="position profiles").add_subparsers(
        dest="pcmd", required=True)
    pos.add_parser("list").set_defaults(fn=cmd_pos_list)
    x = pos.add_parser("show")
    x.add_argument("profile")
    x.set_defaults(fn=cmd_pos_show)
    x = pos.add_parser("new")
    x.add_argument("profile")
    x.add_argument("--vehicle", default="")
    x.add_argument("--zero-ref", default="", help="where the fiducial is, how the rig sits")
    x.set_defaults(fn=cmd_pos_new)
    x = pos.add_parser("copy", help="a new profile with these positions, images and masks")
    x.add_argument("profile")
    x.add_argument("new")
    x.add_argument("--vehicle", default=None)
    x.set_defaults(fn=cmd_pos_copy)
    x = pos.add_parser("save", help="record the current pose (backlash taken up first)")
    x.add_argument("profile")
    x.add_argument("name")
    x.set_defaults(fn=cmd_pos_save)
    x = pos.add_parser("delete", help="one position, or the whole profile (to .trash)")
    x.add_argument("profile")
    x.add_argument("name", nargs="?")
    x.set_defaults(fn=cmd_pos_delete)
    x = pos.add_parser("goto")
    x.add_argument("profile")
    x.add_argument("name")
    x.add_argument("--speed", type=float, default=None)
    x.set_defaults(fn=cmd_pos_goto)
    x = pos.add_parser("order", help="set the visiting order")
    x.add_argument("profile")
    x.add_argument("names", nargs="+")
    x.set_defaults(fn=cmd_pos_order)
    x = pos.add_parser("gaze", help="record the current pose as the forward gaze")
    x.add_argument("profile")
    x.set_defaults(fn=cmd_pos_gaze)
    x = pos.add_parser("run", help="visit every position in order")
    x.add_argument("profile")
    x.add_argument("--dwell", type=float, default=0.0, help="seconds at each position")
    x.add_argument("--speed", type=float, default=None)
    x.set_defaults(fn=cmd_pos_run)

    sd = sub.add_parser("study", help="record, follow and fetch studies").add_subparsers(
        dest="scmd", required=True)
    x = sd.add_parser("start", help="record a study of every position in a profile")
    x.add_argument("profile")
    x.add_argument("--lighting", choices=("dark", "daylight"), required=True)
    x.add_argument("--operator", default="")
    x.add_argument("--notes", default="")
    x.add_argument("--camera-profile", default=None)
    x.add_argument("--no-sweep", action="store_true", help="L_b from each frame (provisional)")
    x.add_argument("--no-check", action="store_true", help="no repeat of the first position")
    x.add_argument("--wait", action="store_true", help="follow it to the end")
    x.add_argument("--timeout", type=float, default=7200)
    x.set_defaults(fn=cmd_study_start)
    x = sd.add_parser("list")
    x.add_argument("query", nargs="?", default="")
    x.set_defaults(fn=cmd_study_list)
    x = sd.add_parser("show")
    x.add_argument("study")
    x.add_argument("--json", action="store_true")
    x.set_defaults(fn=cmd_study_show)
    x = sd.add_parser("wait", help="follow a running study to the end")
    x.add_argument("study")
    x.add_argument("--timeout", type=float, default=7200)
    x.set_defaults(fn=cmd_study_wait)
    x = sd.add_parser("reanalyse", help="analyse the recorded brackets again")
    x.add_argument("study")
    x.add_argument("--wait", action="store_true")
    x.add_argument("--timeout", type=float, default=7200)
    x.set_defaults(fn=cmd_study_reanalyse)
    x = sd.add_parser("download")
    x.add_argument("study")
    x.add_argument("what", choices=("results", "raw", "pdf", "excel"))
    x.add_argument("-o", "--output", default=None)
    x.set_defaults(fn=cmd_study_download)
    x = sub.add_parser("shutdown", help="power the Pi off cleanly (only on the rig)")
    x.add_argument("--yes", action="store_true")
    x.set_defaults(fn=cmd_shutdown)

    args = p.parse_args(argv)
    if getattr(args, "selftest", False):
        return cmd_selftest(args)
    try:
        return args.fn(Client(args.url), args)
    except RigCtlError as exc:
        print(f"rigctl: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        try:
            Client(args.url, timeout=2).post("/api/mount/stop")
        except RigCtlError:
            pass
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
