"""How the server and the Pi are doing: memory, threads, temperature, undervoltage.

The rig froze once after hours of running (2026-11-26) and left nothing to read: nobody knew
whether the server had grown, the Pi had run out of memory, or the supply had sagged. So the
server writes one line of these figures to its log every `HEALTH_LOG_S` (a persistent journal
keeps them over the reboot, deploy/README.md §8), warns when the Pi runs short of memory, and
`/api/system` returns the same snapshot.

Linux only (/proc, /sys, vcgencmd); on a desk the fields it cannot read are None. Nothing here
holds the hardware, and nothing raises: a health check must not take the server down.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

HEALTH_LOG_S = 600.0       # one line in the log every 10 min
LOW_MEMORY_FRACTION = 0.10  # warn below this much of the Pi's memory available

# vcgencmd get_throttled bits (Raspberry Pi firmware)
_THROTTLE_BITS = {0: "undervoltage now", 1: "arm frequency capped now", 2: "throttled now",
                  3: "soft temperature limit now", 16: "undervoltage since boot",
                  17: "arm frequency capped since boot", 18: "throttled since boot",
                  19: "soft temperature limit since boot"}


def _kb_field(path: Path, name: str) -> int | None:
    """A 'Name:   123 kB' line of a /proc file, in kB."""
    try:
        for line in path.read_text().splitlines():
            if line.startswith(name + ":"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def _mb(kb: int | None) -> float | None:
    return None if kb is None else round(kb / 1024, 1)


def _rss_mb(pid: int | str) -> float | None:
    return _mb(_kb_field(Path(f"/proc/{pid}/status"), "VmRSS"))


def throttled() -> dict[str, Any] | None:
    """The firmware's undervoltage / throttling flags, or None off a Pi."""
    exe = shutil.which("vcgencmd")
    if exe is None:
        return None
    try:
        out = subprocess.run([exe, "get_throttled"], capture_output=True, text=True,
                             timeout=2, check=True).stdout
        value = int(out.strip().split("=")[1], 16)
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None
    return {"raw": hex(value),
            "flags": [text for bit, text in _THROTTLE_BITS.items() if value & (1 << bit)]}


def snapshot(with_throttled: bool = True) -> dict[str, Any]:
    """Figures for the log and /api/system. Fields that cannot be read are None."""
    meminfo = Path("/proc/meminfo")
    total, avail = _kb_field(meminfo, "MemTotal"), _kb_field(meminfo, "MemAvailable")
    swap_total, swap_free = _kb_field(meminfo, "SwapTotal"), _kb_field(meminfo, "SwapFree")
    temp = None
    try:
        temp = round(int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000, 1)
    except (OSError, ValueError):
        pass
    try:
        load = round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        load = None
    workers = {}
    for child in multiprocessing.active_children():   # the analysis worker
        if child.pid is not None:
            workers[str(child.pid)] = _rss_mb(child.pid)
    return {
        "server_rss_mb": _rss_mb("self"),
        "server_threads": threading.active_count(),
        "worker_rss_mb": workers,
        "mem_total_mb": _mb(total),
        "mem_available_mb": _mb(avail),
        "swap_used_mb": (_mb(swap_total - swap_free)
                         if swap_total is not None and swap_free is not None else None),
        "load_1min": load,
        "cpu_temp_c": temp,
        "throttled": throttled() if with_throttled else None,
    }


def low_memory(snap: dict[str, Any]) -> bool:
    total, avail = snap.get("mem_total_mb"), snap.get("mem_available_mb")
    return bool(total and avail is not None and avail < LOW_MEMORY_FRACTION * total)


def log_line(snap: dict[str, Any]) -> str:
    """One compact log line; grep the journal for 'health:' to follow it over hours."""
    th = snap.get("throttled")
    workers = ",".join(f"{v}" for v in snap["worker_rss_mb"].values()) or "-"
    return (f"health: server {snap['server_rss_mb']} MB, {snap['server_threads']} threads, "
            f"worker {workers} MB, available {snap['mem_available_mb']} of "
            f"{snap['mem_total_mb']} MB, swap used {snap['swap_used_mb']} MB, "
            f"load {snap['load_1min']}, {snap['cpu_temp_c']} C, "
            f"throttled {th['raw'] if th else None}"
            + (f" ({'; '.join(th['flags'])})" if th and th["flags"] else ""))


def run_logger(stop: threading.Event, period_s: float = HEALTH_LOG_S) -> None:
    """Thread body: a line at start, then every period, until `stop` is set."""
    while True:
        try:
            snap = snapshot()
            if low_memory(snap):
                log.warning("%s - the Pi is low on memory", log_line(snap))
            else:
                log.info("%s", log_line(snap))
        except Exception as exc:  # noqa: BLE001 - never take the server down
            log.warning("health check failed: %s", exc)
        if stop.wait(period_s):
            return
