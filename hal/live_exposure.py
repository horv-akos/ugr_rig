"""Shutter for live view, metered on the live-view frames themselves.

With the dial on M the body shows live view at the brightness the set exposure would give.
Nothing else chooses a shutter for it: after a bracket it stands on the slowest bracket frame
(4 s), after power-up on whatever was dialled in, and a lit scene is then overexposed. ISO and
aperture belong to k and the profile, so only the shutter moves. Every bracket frame sets its
own shutter, so nothing here reaches a measurement.

The frames are gamma coded with the body's tone curve, so an error in EV is only approximate:
the loop steps, lets the body catch up, and meters again. Two limits, the lower one wins: the
mean of the frame at mid grey, and all but `highlight_fraction` of it below clipping, so a lit
panel in a dark cab is not burnt out for the sake of the trim.
"""

from __future__ import annotations

import io
import math

import numpy as np

from .camera_config import LiveViewConfig, parse_shutter

GAMMA = 2.2
CLIPPED = 250        # grey level from which a pixel says only "too bright"
HEADROOM = 240       # level the brightest kept pixels may reach
MAX_STEP_EV = 3.0    # a clipped or black frame does not say how far off it is
GAIN = 0.8           # below 1: the body's tone curve is steeper than GAMMA in the mid tones
_LIN = (np.arange(256) / 255.0) ** GAMMA


def error_ev(jpeg: bytes, cfg: LiveViewConfig) -> float | None:
    """EV to add to the exposure (negative: too bright), or None if the frame is no image."""
    from PIL import Image

    try:
        im = Image.open(io.BytesIO(jpeg))
        im.draft("L", (im.width // 4, im.height // 4))   # decode small: cheap on the Pi
        hist = np.asarray(im.convert("L").histogram(), float)
    except (OSError, SyntaxError, ValueError):
        return None
    hist /= hist.sum()
    ev = math.log2(_LIN[cfg.target_level] / max(float(hist @ _LIN), 1e-5))
    level = min(int(np.searchsorted(np.cumsum(hist), 1.0 - cfg.highlight_fraction)), 255)
    if level >= CLIPPED:
        ev = min(ev, -1.0 - 2.0 * float(hist[CLIPPED:].sum()))
    elif level > 0:
        ev = min(ev, math.log2(_LIN[HEADROOM] / _LIN[level]))
    return max(-MAX_STEP_EV, min(MAX_STEP_EV, ev))


class LiveExposure:
    """Feedback loop: `update()` takes each frame and returns a new shutter label or None."""

    def __init__(self, cfg: LiveViewConfig) -> None:
        self.cfg = cfg
        self.label = cfg.shutter_start
        self.settled = False               # the last metered frame needed no change
        self.at_limit: str | None = None   # "bright" / "dark": off target at the end of the range
        self._skip = 0

    def start(self, choices: list[str]) -> str | None:
        """Shutter to set before live view comes up: the one it last ran at."""
        self.settled = False
        self._skip = self.cfg.settle_frames
        return self._nearest(parse_shutter(self.label), choices)

    def changed(self, label: str) -> None:
        """The body took `label`."""
        self.label = label
        self._skip = self.cfg.settle_frames

    def update(self, jpeg: bytes, choices: list[str]) -> str | None:
        if self._skip > 0:                 # shot before the last change took effect
            self._skip -= 1
            return None
        ev = error_ev(jpeg, self.cfg)
        self.at_limit = None
        self.settled = ev is None or abs(ev) <= self.cfg.deadband_ev
        if self.settled:
            return None
        want = self._nearest(parse_shutter(self.label) * 2.0 ** (GAIN * ev), choices)
        if want is None or want == self.label:
            self.settled = True            # as close as the speeds allow
            self.at_limit = "bright" if ev < 0 else "dark"
            return None
        return want

    def _nearest(self, t: float, choices: list[str]) -> str | None:
        """The offered speed closest to t within the configured range (bulb left out)."""
        lo = parse_shutter(self.cfg.shutter_fastest) * 0.9
        hi = parse_shutter(self.cfg.shutter_slowest) * 1.1
        best, best_err = None, math.inf
        for c in choices:
            try:
                s = parse_shutter(c)
            except ValueError:
                continue
            err = abs(math.log2(s / t))
            if lo <= s <= hi and err < best_err:
                best, best_err = c, err
        return best
