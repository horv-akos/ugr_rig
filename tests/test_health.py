"""service/health.py: the figures the server logs so a freeze leaves evidence."""

from __future__ import annotations

import subprocess
import threading
import types

from service import health


def test_snapshot_never_raises_and_has_every_field():
    snap = health.snapshot()
    for key in ("server_rss_mb", "server_threads", "worker_rss_mb", "mem_total_mb",
                "mem_available_mb", "swap_used_mb", "load_1min", "cpu_temp_c", "throttled"):
        assert key in snap
    assert snap["server_threads"] >= 1
    assert health.log_line(snap).startswith("health: server ")


def test_proc_fields(tmp_path):
    f = tmp_path / "meminfo"
    f.write_text("MemTotal:        7999000 kB\nMemAvailable:     512000 kB\n")
    assert health._kb_field(f, "MemTotal") == 7999000
    assert health._kb_field(f, "SwapTotal") is None
    assert health._kb_field(tmp_path / "missing", "MemTotal") is None
    assert health.low_memory({"mem_total_mb": 7811.5, "mem_available_mb": 500.0})
    assert not health.low_memory({"mem_total_mb": 7811.5, "mem_available_mb": 4000.0})
    assert not health.low_memory({"mem_total_mb": None, "mem_available_mb": None})


def test_throttled_flags(monkeypatch):
    monkeypatch.setattr(health.shutil, "which", lambda name: "/usr/bin/vcgencmd")
    monkeypatch.setattr(health.subprocess, "run", lambda *a, **k: types.SimpleNamespace(
        stdout="throttled=0x50005\n"))
    th = health.throttled()
    assert th["raw"] == "0x50005"
    assert th["flags"] == ["undervoltage now", "throttled now", "undervoltage since boot",
                           "throttled since boot"]

    def broken(*a, **k):
        raise subprocess.TimeoutExpired("vcgencmd", 2)
    monkeypatch.setattr(health.subprocess, "run", broken)
    assert health.throttled() is None
    monkeypatch.setattr(health.shutil, "which", lambda name: None)
    assert health.throttled() is None


def test_logger_logs_and_stops(caplog):
    stop = threading.Event()
    t = threading.Thread(target=health.run_logger, args=(stop, 60.0), daemon=True)
    with caplog.at_level("INFO", logger="service.health"):
        t.start()
        for _ in range(200):
            if any("health:" in r.message for r in caplog.records):
                break
            stop.wait(0.01)
        stop.set()
        t.join(2.0)
    assert not t.is_alive()
    assert any("health:" in r.message for r in caplog.records)
