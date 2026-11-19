"""Run the rig server: one process that owns the mount and serves the HTTP API.

    python -m web                                   simulated head and camera, 127.0.0.1:8000
    python -m web --camera none                     simulated head, no live view
    python -m web --backend pigpio --host 0.0.0.0   on the Pi (camera: the 2000D), over its WiFi

The movement calibration page is at /movement (/ redirects there for now).

Once the socket is bound it prints the URLs it can be opened on (with --host 0.0.0.0, one per
network interface, the SSH address first), so a headless Pi tells you where to point the
laptop's browser. -v also turns on uvicorn's request log.

On the Pi pigpiod must be running (hal/README.md). Until hal/tmc2209.py configures the
drivers over UART, PAN runs 1/8 and TILT 1/32 microstepping, so do not move the real head
with this yet.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from service.controller import DEFAULT_DATA_DIR, RigController  # noqa: E402
from web.api import create_app  # noqa: E402
from web.netinfo import is_loopback, server_urls, ssh_server_ip  # noqa: E402

log = logging.getLogger("web")


def banner(args: argparse.Namespace, rig: RigController) -> str:
    """What the operator needs on a headless Pi: where to open it, and what is running."""
    st = rig.status()
    lines = ["", "UGR rig server running", ""]
    urls = server_urls(args.host, args.port, ssh_ip=ssh_server_ip())
    width = max(len(u) for u, _ in urls)
    lines += [f"  {u:<{width}}   {note}".rstrip() for u, note in urls]
    if is_loopback(args.host) and ssh_server_ip():
        lines += ["", "  You are on SSH but the server only listens on this machine.",
                  f"  To open it from your laptop: python -m web --host 0.0.0.0 --port {args.port}"]
    backend = args.backend + (f" (time scale {args.time_scale:g})" if args.backend == "sim" else "")
    lines += ["",
              f"  backend   {backend}",
              f"  mount     {args.config or 'config/mount.toml (default)'}",
              f"  camera    {getattr(args, 'camera', None) or rig.live.source} "
              f"(live view {st['live_view']['state']})",
              f"  data      {rig.data_dir}",
              f"  head      drivers {'on' if st['enabled'] else 'off'}, "
              f"{'zeroed' if st['zeroed'] else 'not zeroed'}",
              f"  page      {urls[0][0]}/movement",
              f"  API       {urls[0][0]}/api/status",
              "", "  Ctrl+C to stop.", ""]
    return "\n".join(lines)


def run_server(app, args: argparse.Namespace, rig: RigController) -> None:
    import uvicorn

    class Server(uvicorn.Server):
        # Print only once the socket is bound: a busy port exits inside startup().
        async def startup(self, sockets=None) -> None:
            await super().startup(sockets=sockets)
            if self.started and not self.should_exit:
                print(banner(args, rig), flush=True)

    # Live-view streams never end on their own; without a grace timeout Ctrl+C would wait
    # for every open browser tab.
    config = uvicorn.Config(app, host=args.host, port=args.port,
                            log_level="info" if args.verbose else "warning",
                            access_log=args.verbose, timeout_graceful_shutdown=2)
    Server(config).run()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m web", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--backend", default="sim", choices=["sim", "pigpio"])
    p.add_argument("--time-scale", type=float, default=1.0,
                   help="sim only: 1.0 moves in real time, 0 instantly")
    p.add_argument("--camera", default=None, choices=["sim", "gphoto2", "none"],
                   help="live view source (default: sim with --backend sim, else gphoto2)")
    p.add_argument("--config", default=None, help="path to mount.toml")
    p.add_argument("--data", default=str(DEFAULT_DATA_DIR),
                   help="data directory: profiles, studies, state (default: %(default)s)")
    p.add_argument("--host", default="127.0.0.1",
                   help="0.0.0.0 on the Pi to serve its WiFi (default: %(default)s)")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("-v", "--verbose", action="store_true",
                   help="debug logging and one line per HTTP request")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    kwargs = {"time_scale": args.time_scale} if args.backend == "sim" else {}
    if args.camera is None:
        args.camera = "sim" if args.backend == "sim" else "gphoto2"
    if args.backend == "pigpio":
        log.warning("pigpio backend: hal/tmc2209.py does not exist yet, so the drivers are "
                    "not configured (PAN 1/8, TILT 1/32 microstepping). Do not move the real head.")
    rig = RigController.from_config(args.backend, args.data, args.config, camera=args.camera,
                                    **kwargs)
    try:
        run_server(create_app(rig), args, rig)
    finally:
        rig.close()  # drivers released on every exit path
        print("rig server stopped, drivers released", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
