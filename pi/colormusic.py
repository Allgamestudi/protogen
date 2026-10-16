"""Light show mode ("music"): zones, rings, plasma, flash, or auto (cycles).

Input is the 16 band levels and the beat. Output is one 64x32 panel; the
renderer mirrors it and applies the power limit.
"""
import math

import numpy as np

STYLES = ("auto", "zones", "rings", "plasma", "flash")
AUTO_ORDER = ("rings", "zones", "plasma", "flash")
AUTO_BEATS = 16             # beats before "auto" moves on
AUTO_SECONDS = 25.0         # ... or this long, when there is no beat
RISE, FALL = 30.0, 5.0      # how fast a level follows the sound up / down, 1/s
RING_SPEED = 30.0           # pixels per second
RING_WIDTH = 2.6            # pixels
MAX_RINGS = 8
ZONE_COLOURS = np.array([(255, 30, 20), (40, 255, 60), (40, 90, 255)], np.float32)


def hsv(h, v=1.0):
    """Fully saturated colour(s) of hue h (0..1, any shape) -> float RGB 0..1."""
    h = np.asarray(h, np.float32)[..., None]
    return np.clip(np.abs((h * 6.0 + np.array([0.0, 4.0, 2.0], np.float32)) % 6.0 - 3.0) - 1.0, 0.0, 1.0) * v


def levels(bars):
    """Loudness of bass, middle and treble (three numbers 0..1)."""
    n = len(bars)
    a, b = max(1, n // 4), max(2, n * 5 // 8)
    parts = (bars[:a], bars[a:b], bars[b:])
    # half average, half loudest band: one strong note already lights its zone
    return np.array([0.5 * p.mean() + 0.5 * p.max() for p in parts], np.float32)


class Show:
    def __init__(self, w=64, h=32):
        self.w, self.h = w, h
        y, x = np.mgrid[0:h, 0:w].astype(np.float32)
        self.x, self.y = x, y
        self.r = np.hypot(x - (w - 1) / 2.0, y - (h - 1) / 2.0)
        self.r_max = float(self.r.max())
        self.level = np.zeros(3, np.float32)    # smoothed bass, middle, treble
        self.flash = 0.0                        # fades quickly after every beat
        self.glow = 0.0                         # the same, fading slowly
        self.hue = 0.0                          # moves on with every beat
        self.rings = []                         # [radius, strength, hue]
        self.phase = 0.0
        self.auto_at, self.auto_beats, self.auto_time = 0, 0, 0.0
        # how much each zone lights each column: soft edges, so the colours blend
        centres = (np.arange(3, dtype=np.float32) + 0.5) * w / 3.0
        self.zone = np.clip(1.25 - np.abs(x[None] - centres[:, None, None]) / (w / 3.0), 0.0, 1.0) ** 2
        self.height = (h - 1 - y) / h           # 0 at the bottom row, almost 1 at the top

    # what the music is doing
    def _listen(self, bars, beat, dt):
        raw = levels(np.asarray(bars, np.float32))
        rate = np.where(raw > self.level, RISE, FALL)
        self.level += (raw - self.level) * (1.0 - np.exp(-dt * rate))
        self.flash *= math.exp(-dt * 5.0)
        self.glow *= math.exp(-dt * 2.2)
        if beat > 0.0:
            self.flash = max(self.flash, min(1.0, beat))
            self.glow = max(self.glow, 0.6 + 0.4 * min(1.0, beat))
            self.hue = (self.hue + 0.381966) % 1.0      # golden step: neighbours never look alike
            self.rings.append([0.0, 0.5 + 0.5 * min(1.0, beat), self.hue])
            del self.rings[:-MAX_RINGS]
            self.auto_beats += 1
        self.auto_time += dt
        if self.auto_beats >= AUTO_BEATS or self.auto_time >= AUTO_SECONDS:
            self.auto_at = (self.auto_at + 1) % len(AUTO_ORDER)
            self.auto_beats, self.auto_time = 0, 0.0
        speed = RING_SPEED + 30.0 * float(self.level[0])
        for ring in self.rings:
            ring[0] += speed * dt
            ring[1] *= math.exp(-dt * 1.4)
        self.rings = [r for r in self.rings if r[0] < self.r_max + 3 * RING_WIDTH and r[1] > 0.03]
        self.phase += dt * (0.5 + 3.0 * float(self.level.mean()))

    # the styles: each returns float RGB 0..1
    def _zones(self):
        out = np.zeros((self.h, self.w, 3), np.float32)
        for i in range(3):
            lv = float(self.level[i])
            column = np.clip((lv * 1.5 - self.height) * 4.0, 0.0, 1.0) * 0.85 + lv * 0.15
            out += (self.zone[i] * column)[..., None] * (ZONE_COLOURS[i] / 255.0)
        return out + self.flash * 0.18

    def _rings(self):
        bass = float(self.level[0])
        core = np.exp(-(self.r / (2.5 + 9.0 * bass)) ** 2) * (0.25 + bass)
        out = core[..., None] * hsv(self.hue)
        for radius, strength, hue in self.rings:
            band = np.exp(-((self.r - radius) / RING_WIDTH) ** 2) * strength
            out += band[..., None] * hsv(hue)
        return out

    def _plasma(self):
        p = self.phase
        v = (np.sin(self.x * 0.16 + p) + np.sin(self.y * 0.22 - p * 1.3)
             + np.sin((self.x + self.y) * 0.11 + p * 0.7) + np.sin(self.r * 0.25 - p * 2.0))
        loud = float(self.level.max())
        return hsv((v * 0.12 + self.hue) % 1.0) * min(1.0, 0.18 + 0.9 * loud + 0.35 * self.flash)

    def _flash(self):
        glow = 1.0 - 0.6 * self.r / self.r_max          # brighter in the middle
        amount = min(1.0, self.glow + 0.4 * float(self.level.max()))
        return (glow * amount)[..., None] * hsv(self.hue)

    def frame(self, bars, beat=0.0, dt=1.0 / 30.0, style="auto"):
        self._listen(bars, float(beat), min(0.2, max(0.0, float(dt))))
        if style not in STYLES or style == "auto":
            style = AUTO_ORDER[self.auto_at]
        out = getattr(self, "_" + style)()
        return (np.clip(out, 0.0, 1.0) * 255.0).astype(np.uint8)


_show = None


def render(bars, beat=0.0, dt=1.0 / 30.0, style="auto", w=64, h=32):
    """One frame of the show. Keeps its own memory between calls."""
    global _show
    if _show is None or (_show.w, _show.h) != (w, h):
        _show = Show(w, h)
    return _show.frame(bars, beat, dt, style)
