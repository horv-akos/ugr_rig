"""The synthetic end-to-end check from `ugrctl selftest`, under pytest."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.ugrctl import main  # noqa: E402


def test_selftest():
    assert main(["--camera", "sim", "selftest"]) == 0
