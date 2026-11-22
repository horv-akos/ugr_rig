"""Shared test setup.

Every RigController starts its own analysis worker process on first use (a spawned interpreter
importing numpy, scipy and matplotlib: ~1 s). The tests make a dozen rigs that analyse, so here
they share one worker for the session. A test that breaks the pool gets a fresh one. The
per-rig pool itself is two lines (`RigController.pool`) and runs in the selftests and the
server.
"""

from __future__ import annotations

import multiprocessing
from concurrent.futures import ProcessPoolExecutor

import pytest

from service.controller import RigController

_shared: ProcessPoolExecutor | None = None


def _pool(self) -> ProcessPoolExecutor:
    global _shared
    if _shared is None or getattr(_shared, "_broken", False):
        _shared = ProcessPoolExecutor(max_workers=1,
                                      mp_context=multiprocessing.get_context("spawn"))
    return _shared


@pytest.fixture(autouse=True, scope="session")
def shared_analysis_worker():
    original = RigController.pool
    RigController.pool = _pool
    yield
    RigController.pool = original
    if _shared is not None:
        _shared.shutdown(wait=True, cancel_futures=True)
