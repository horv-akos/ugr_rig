"""Run the rig server: one process that owns the mount and serves the HTTP API.

    python -m web                                   simulated head, http://127.0.0.1:8000
    python -m web --time-scale 1                    simulated head moving in real time
    python -m web --backend pigpio --host 0.0.0.0   on the Pi, reachable over its WiFi

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

log = logging.getLogger("web")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m web", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--backend", default="sim", choices=["sim", "pigpio"])
    p.add_argument("--time-scale", type=float, default=1.0,
                   help="sim only: 1.0 moves in real time, 0 instantly")
    p.add_argument("--config", default=None, help="path to mount.toml")
    p.add_argument("--data", default=str(DEFAULT_DATA_DIR),
                   help="data directory: profiles, studies, state (default: %(default)s)")
    p.add_argument("--host", default="127.0.0.1",
                   help="0.0.0.0 on the Pi to serve its WiFi (default: %(default)s)")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    import uvicorn

    kwargs = {"time_scale": args.time_scale} if args.backend == "sim" else {}
    if args.backend == "pigpio":
        log.warning("pigpio backend: hal/tmc2209.py does not exist yet, so the drivers are "
                    "not configured (PAN 1/8, TILT 1/32 microstepping). Do not move the real head.")
    rig = RigController.from_config(args.backend, args.data, args.config, **kwargs)
    try:
        uvicorn.run(create_app(rig), host=args.host, port=args.port, log_level="warning")
    finally:
        rig.close()  # drivers released on every exit path
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
