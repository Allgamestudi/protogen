"""Boot animation for the LED matrix.

Sequence:
  1. SCANLINES  - CRT power-on: horizontal scanlines sweep + static noise
  2. CONSOLE    - fake boot log types out with glitch-scramble effect
  3. RAIN       - Matrix-style digital rain fills the screen
  4. ASSEMBLE   - face pixels fly in from random positions
  5. PULSE      - face flares bright then settles to normal colour
  6. done       - hands off to normal render loop
"""
import math
import random
import time
import numpy as np

import config

# per-setting overrides (can be driven from state["settings"])
SCANLINES_DURATION  = 0.9
CONSOLE_LINE_TIME   = 0.28
LOADBAR_DURATION    = 0.0   # unused - kept for compat
GLITCH_DURATION     = 0.0   # unused - kept for compat
RAIN_DURATION       = 1.2
ASSEMBLE_DURATION   = 1.4
PULSE_DURATION      = 0.55

# colour palette
COL_CONSOLE  = (0,   220,  70)
COL_DIM      = (30,  100,  40)
COL_OK       = (160, 255, 160)
COL_RAIN_HI  = (180, 255, 180)
COL_RAIN_MID = (0,   200,  60)
COL_RAIN_LO  = (0,    80,  20)
COL_FACE     = (255,  30,  30)
COL_PULSE    = (255, 160,  60)

BOOT_LINES = [
    "INIT SYS",
    "MATRIX DRV",
    "ESP32 LINK",
    "MIC READY",
    "FACE ENG",
    "BOOT OK",
]

# stage names
STAGE_SCANLINES = "scanlines"
STAGE_CONSOLE   = "console"
STAGE_LOADBAR   = "loadbar"   # alias kept for compat
STAGE_RAIN      = "rain"
STAGE_ASSEMBLE  = "assemble"
STAGE_PULSE     = "pulse"
STAGE_GLITCH    = "glitch"    # alias kept for compat
STAGE_DONE      = "done"

# ==========================================================================
# 4x6 PIXEL FONT (same as before)
# ==========================================================================
FONT_4X6 = {
    ' ': [0, 0, 0, 0, 0, 0],
    'A': [0b0110, 0b1001, 0b1111, 0b1001, 0b1001, 0],
    'B': [0b1110, 0b1001, 0b1110, 0b1001, 0b1110, 0],
    'C': [0b0111, 0b1000, 0b1000, 0b1000, 0b0111, 0],
    'D': [0b1110, 0b1001, 0b1001, 0b1001, 0b1110, 0],
    'E': [0b1111, 0b1000, 0b1110, 0b1000, 0b1111, 0],
    'F': [0b1111, 0b1000, 0b1110, 0b1000, 0b1000, 0],
    'G': [0b0111, 0b1000, 0b1011, 0b1001, 0b0111, 0],
    'H': [0b1001, 0b1001, 0b1111, 0b1001, 0b1001, 0],
    'I': [0b1110, 0b0100, 0b0100, 0b0100, 0b1110, 0],
    'J': [0b0111, 0b0010, 0b0010, 0b1010, 0b0100, 0],
    'K': [0b1001, 0b1010, 0b1100, 0b1010, 0b1001, 0],
    'L': [0b1000, 0b1000, 0b1000, 0b1000, 0b1111, 0],
    'M': [0b1001, 0b1111, 0b1111, 0b1001, 0b1001, 0],
    'N': [0b1001, 0b1101, 0b1101, 0b1011, 0b1001, 0],
    'O': [0b0110, 0b1001, 0b1001, 0b1001, 0b0110, 0],
    'P': [0b1110, 0b1001, 0b1110, 0b1000, 0b1000, 0],
    'Q': [0b0110, 0b1001, 0b1001, 0b1011, 0b0111, 0],
    'R': [0b1110, 0b1001, 0b1110, 0b1010, 0b1001, 0],
    'S': [0b0111, 0b1000, 0b0110, 0b0001, 0b1110, 0],
    'T': [0b1111, 0b0100, 0b0100, 0b0100, 0b0100, 0],
    'U': [0b1001, 0b1001, 0b1001, 0b1001, 0b0110, 0],
    'V': [0b1001, 0b1001, 0b1001, 0b0110, 0b0110, 0],
    'W': [0b1001, 0b1001, 0b1001, 0b1111, 0b0110, 0],
    'X': [0b1001, 0b0110, 0b0110, 0b0110, 0b1001, 0],
    'Y': [0b1001, 0b1001, 0b0110, 0b0100, 0b0100, 0],
    'Z': [0b1111, 0b0001, 0b0110, 0b1000, 0b1111, 0],
    '0': [0b0110, 0b1001, 0b1001, 0b1001, 0b0110, 0],
    '1': [0b0100, 0b1100, 0b0100, 0b0100, 0b1110, 0],
    '2': [0b0110, 0b1001, 0b0010, 0b0100, 0b1111, 0],
    '3': [0b1110, 0b0001, 0b0110, 0b0001, 0b1110, 0],
    '4': [0b1001, 0b1001, 0b1111, 0b0001, 0b0001, 0],
    '5': [0b1111, 0b1000, 0b1110, 0b0001, 0b1110, 0],
    '6': [0b0110, 0b1000, 0b1110, 0b1001, 0b0110, 0],
    '7': [0b1111, 0b0001, 0b0010, 0b0100, 0b0100, 0],
    '8': [0b0110, 0b1001, 0b0110, 0b1001, 0b0110, 0],
    '9': [0b0110, 0b1001, 0b0111, 0b0001, 0b0110, 0],
    '.': [0,      0,      0,      0,      0b0100, 0],
    ',': [0,      0,      0,      0b0100, 0b1000, 0],
    ':': [0,      0b0100, 0,      0b0100, 0,      0],
    '!': [0b0100, 0b0100, 0b0100, 0,      0b0100, 0],
    '?': [0b1110, 0b0001, 0b0110, 0,      0b0100, 0],
    '-': [0,      0,      0b1111, 0,      0,      0],
    '_': [0,      0,      0,      0,      0b1111, 0],
    '/': [0b0001, 0b0010, 0b0100, 0b1000, 0,      0],
    '\\': [0b1000, 0b0100, 0b0010, 0b0001, 0,     0],
    '|': [0b0100, 0b0100, 0b0100, 0b0100, 0b0100, 0],
    '%': [0b1001, 0b0010, 0b0100, 0b1001, 0,      0],
    '>': [0b1000, 0b0100, 0b0010, 0b0100, 0b1000, 0],
    '<': [0b0001, 0b0010, 0b0100, 0b0010, 0b0001, 0],
}
CHAR_W = 4
CHAR_H = 6
STRIDE = 5
LINE_H = CHAR_H + 1


def text_width(s): return max(0, len(s) * STRIDE - 1)


def draw_text(img, x, y, text, color):
    # Silently skip if buffer is read-only (e.g. PIL-backed view after transform)
    if not img.flags.writeable:
        return x
    H, W = img.shape[:2]
    col  = np.array(color, dtype=np.uint8)
    try:
        for ch in text.upper():
            g = FONT_4X6.get(ch, FONT_4X6['?'])
            if 0 <= y and x + CHAR_W <= W:
                for r in range(CHAR_H):
                    b = g[r]
                    py = y + r
                    if py < 0 or py >= H or not b:
                        continue
                    if b & 0b1000 and 0 <= x   < W: img[py, x  ] = col
                    if b & 0b0100 and 0 <= x+1 < W: img[py, x+1] = col
                    if b & 0b0010 and 0 <= x+2 < W: img[py, x+2] = col
                    if b & 0b0001 and 0 <= x+3 < W: img[py, x+3] = col
            x += STRIDE
            if x >= W:
                break
    except (ValueError, RuntimeError):
        pass
    return x


# glitch charset for scramble effect
_GLITCH_CHARS = "0123456789ABCDEFX#@%!?"


# ==========================================================================
# Stage 1 - CRT scanline power-on
# ==========================================================================
def render_scanlines(t: float) -> np.ndarray:
    """Horizontal scanlines sweep down like a CRT warming up, with noise."""
    img = np.zeros((config.H, config.W, 3), dtype=np.uint8)
    p   = min(1.0, t / SCANLINES_DURATION)

    rng = random.Random(int(t * 500) & 0xFFFF)

    # noise field
    for y in range(config.H):
        scan_p = y / (config.H - 1)
        if scan_p > p:
            break
        brightness = max(0.0, 1.0 - abs(scan_p - p) * 18)
        for x in range(config.W):
            n = rng.random()
            if n > 0.55:
                v = int(brightness * rng.randint(40, 180))
                g = int(v * rng.uniform(0.8, 1.2))
                img[y, x] = (0, min(255, g), int(v * 0.3))

    # bright sweep line
    sl = int(p * (config.H - 1))
    for dy in range(-1, 2):
        sy = sl + dy
        if 0 <= sy < config.H:
            bright = 255 if dy == 0 else 100
            img[sy, :] = np.maximum(img[sy, :], [0, bright, int(bright * 0.4)])

    return img


# ==========================================================================
# Stage 2 - glitch console
# ==========================================================================
def render_console(elapsed: float) -> np.ndarray:
    img = np.zeros((config.H, config.W, 3), dtype=np.uint8)
    n_visible = config.H // LINE_H

    line_idx       = elapsed / CONSOLE_LINE_TIME
    full_lines     = int(line_idx)
    typing_frac    = line_idx - full_lines

    start    = max(0, full_lines + 1 - n_visible)
    end_full = min(full_lines, len(BOOT_LINES))

    y = 0
    for i in range(start, end_full):
        draw_text(img, 1, y, "> " + BOOT_LINES[i], COL_DIM)
        ok_w = text_width("OK")
        draw_text(img, config.W - ok_w - 1, y, "OK", COL_OK)
        y += LINE_H

    if full_lines < len(BOOT_LINES) and y + CHAR_H <= config.H:
        cur    = BOOT_LINES[full_lines]
        n_real = int(len(cur) * min(1.0, typing_frac / 0.75))
        n_glitch_chars = max(0, min(len(cur) - n_real, 3))

        rng    = random.Random(int(elapsed * 60) & 0xFFFF)
        glitch_tail = "".join(rng.choice(_GLITCH_CHARS) for _ in range(n_glitch_chars))
        text = "> " + cur[:n_real] + glitch_tail
        if int(elapsed * 6) & 1:
            text += "_"
        draw_text(img, 1, y, text, COL_CONSOLE)

    return img


# ==========================================================================
# Stage 3 - Matrix digital rain
# ==========================================================================
_RAIN_CHARS = "01ABCDEF#%@!01"

class _RainState:
    """Persistent column drop state so rain looks continuous."""
    def __init__(self):
        rng = random.Random(0xDEAD)
        W   = config.W
        # one drop per column; staggered start positions
        self.pos   = [rng.uniform(-config.H, 0) for _ in range(W)]
        self.speed = [rng.uniform(12, 28)        for _ in range(W)]
        self.tail  = [rng.randint(4, 10)         for _ in range(W)]
        self.chars = [[rng.choice(_RAIN_CHARS) for _ in range(config.H + 16)]
                      for _ in range(W)]
        self._last_t = None

    def step(self, t: float):
        if self._last_t is None:
            self._last_t = t
        dt = min(t - self._last_t, 0.05)
        self._last_t = t
        for x in range(config.W):
            self.pos[x] += self.speed[x] * dt

    def render(self, t: float) -> np.ndarray:
        self.step(t)
        img = np.zeros((config.H, config.W, 3), dtype=np.uint8)
        rng = random.Random(int(t * 30) & 0xFFFF)

        for x in range(config.W):
            head = int(self.pos[x])
            tail = self.tail[x]
            for dy in range(tail + 1):
                y = head - dy
                if y < 0 or y >= config.H:
                    continue
                if dy == 0:
                    img[y, x] = COL_RAIN_HI
                elif dy < tail // 2:
                    img[y, x] = COL_RAIN_MID
                else:
                    img[y, x] = COL_RAIN_LO

        return img


_RAIN = _RainState()


def render_rain(t: float, blend_out: float = 0.0) -> np.ndarray:
    """blend_out in [0,1]: fade rain away toward the end."""
    frame = _RAIN.render(t)
    if blend_out > 0:
        frame = (frame.astype(np.float32) * (1.0 - blend_out)).astype(np.uint8)
    return frame


# ==========================================================================
# Stage 4 - pixel assemble
# ==========================================================================
def _build_pixel_list(eye_mask: np.ndarray, mouth_mask: np.ndarray):
    """Return list of (y, x) face pixels in random order with assigned origins."""
    face = np.maximum(eye_mask, mouth_mask)
    ys, xs = np.where(face > 0)
    pixels = list(zip(ys.tolist(), xs.tolist()))
    rng    = random.Random(0xFACE)
    rng.shuffle(pixels)

    W, H  = config.W, config.H
    origins = []
    for (py, px) in pixels:
        kind = rng.randint(0, 3)
        if kind == 0:
            origins.append((rng.randint(-H, -1), px))          # from top
        elif kind == 1:
            origins.append((rng.randint(H, 2 * H), px))        # from bottom
        elif kind == 2:
            origins.append((py, rng.randint(-W, -1)))           # from left
        else:
            origins.append((py, rng.randint(W, 2 * W)))        # from right
    return pixels, origins


_ASSEMBLE_CACHE: dict = {}   # keyed by id(eye_mask)


def render_assemble(eye_mask: np.ndarray, mouth_mask: np.ndarray,
                    progress: float, t: float) -> np.ndarray:
    """Pixels fly from random offscreen positions to their final positions."""
    cache_key = (id(eye_mask), id(mouth_mask))
    if cache_key not in _ASSEMBLE_CACHE:
        _ASSEMBLE_CACHE[cache_key] = _build_pixel_list(eye_mask, mouth_mask)
    pixels, origins = _ASSEMBLE_CACHE[cache_key]

    img = np.zeros((config.H, config.W, 3), dtype=np.uint8)
    p   = max(0.0, min(1.0, progress))

    n_total  = len(pixels)
    # stagger: first pixel arrives at p=0, last at p=0.85
    for i, ((ty, tx), (oy, ox)) in enumerate(zip(pixels, origins)):
        frac = i / max(1, n_total - 1)
        # eased arrival: pixel i is "done" when p >= frac * 0.85 + 0.15
        arrive = frac * 0.82
        local  = max(0.0, min(1.0, (p - arrive) / 0.18))
        # ease-out cubic
        local  = 1.0 - (1.0 - local) ** 3

        cy = round(oy + (ty - oy) * local)
        cx = round(ox + (tx - ox) * local)

        if 0 <= cy < config.H and 0 <= cx < config.W:
            # trail brightness (dim while in flight, bright when arrived)
            if local >= 1.0:
                img[cy, cx] = COL_FACE
            else:
                v = int(80 + local * 175)
                img[cy, cx] = (v, int(v * 0.12), int(v * 0.12))

    # scanline flicker on top
    sl = int(t * 80) % config.H
    img[sl, :] = np.clip(img[sl, :].astype(int) + 25, 0, 255).astype(np.uint8)

    return img


# ==========================================================================
# Stage 5 - pulse flare
# ==========================================================================
def render_pulse(eye_mask: np.ndarray, mouth_mask: np.ndarray,
                 progress: float) -> np.ndarray:
    """Face flares from white-hot down to its final red colour."""
    face = np.maximum(eye_mask, mouth_mask) > 0
    img  = np.zeros((config.H, config.W, 3), dtype=np.uint8)
    p    = max(0.0, min(1.0, progress))

    # p=0 -> white flash, p=0.5 -> orange, p=1 -> COL_FACE
    if p < 0.5:
        t2 = p / 0.5
        r  = 255
        g  = int(255 * (1 - t2) + COL_PULSE[1] * t2)
        b  = int(200 * (1 - t2) + COL_PULSE[2] * t2)
    else:
        t2 = (p - 0.5) / 0.5
        r  = int(COL_PULSE[0] * (1 - t2) + COL_FACE[0] * t2)
        g  = int(COL_PULSE[1] * (1 - t2) + COL_FACE[1] * t2)
        b  = int(COL_PULSE[2] * (1 - t2) + COL_FACE[2] * t2)

    img[face] = (np.clip([r, g, b], 0, 255)).astype(np.uint8)
    return img


# ==========================================================================
# Boot sequence v3: one wide screen instead of two mirrored halves
# ==========================================================================
# The first stages treat both panels as one screen (text reads normally on
# both, the power-on line runs across the whole face). The face itself is
# drawn on one panel and mirrored, like everything after the boot.
POWER_DURATION   = 0.75
TRACE_DURATION   = 1.6
IGNITE_DURATION  = 0.9
RAIN_V3_DURATION = 0.9

STAGE_POWER  = "power"
STAGE_TRACE  = "trace"
STAGE_IGNITE = "ignite"

COL_BEAM  = (120, 255, 255)
COL_TRACE = (0, 200, 230)
COL_SPARK = (255, 240, 170)


def _wide():
    return config.W * max(1, getattr(config, "CHAIN_LENGTH", 1))


def _mirrored(frame):
    """One panel -> both: the second is the mirror image (as _compose_dual)."""
    if _wide() == frame.shape[1]:
        return frame
    return np.concatenate([frame, frame[:, ::-1]], axis=1)


def _ease_out(x):
    x = max(0.0, min(1.0, x))
    return 1.0 - (1.0 - x) ** 3


def render_power_on(p: float) -> np.ndarray:
    """An old tube switching on: a dot, a line across the whole face, the
    line opens into a flash, the glow dies away."""
    W, H = _wide(), config.H
    img = np.zeros((H, W, 3), dtype=np.float32)
    mid = H // 2
    xs = np.abs(np.arange(W, dtype=np.float32) - (W - 1) / 2.0)
    ys = np.abs(np.arange(H, dtype=np.float32) - (H - 1) / 2.0)[:, None]
    if p < 0.35:                                   # the line grows from the centre
        half = _ease_out(p / 0.35) * W / 2.0
        line = np.clip(half - xs + 1.0, 0.0, 1.0)
        img[mid - 1:mid + 1, :, :] = line[None, :, None] * 255.0
        img[mid - 2:mid + 2, :, 1:] = np.maximum(img[mid - 2:mid + 2, :, 1:], line[None, :, None] * 90.0)
    elif p < 0.65:                                 # it opens up and down
        q = (p - 0.35) / 0.30
        half = 1.0 + _ease_out(q) * H / 2.0
        band = np.clip(half - ys + 1.0, 0.0, 1.0)
        level = 255.0 * (1.0 - 0.75 * q)
        img[:, :, 0] = band * level * (1.0 - q)    # white at first, then the tube's own teal
        img[:, :, 1] = band * level
        img[:, :, 2] = band * level * (1.0 - 0.3 * q)
    else:                                          # afterglow with rolling lines
        q = (p - 0.65) / 0.35
        level = 64.0 * (1.0 - q) ** 2
        roll = 0.6 + 0.4 * np.sin((np.arange(H, dtype=np.float32)[:, None] + p * 90.0) * 1.3)
        img[:, :, 1] = level * roll
        img[:, :, 2] = level * 0.7 * roll
    return np.clip(img, 0, 255).astype(np.uint8)


def render_console_wide(elapsed: float, total: float) -> np.ndarray:
    """Boot log on the first panel, a progress read-out on the second."""
    W, H = _wide(), config.H
    left = render_console(elapsed)
    if W == left.shape[1]:
        return left
    right = np.zeros((H, W - left.shape[1], 3), dtype=np.uint8)
    rw = right.shape[1]
    done = max(0.0, min(1.0, elapsed / max(total, 1e-6)))
    # big percentage: the 4x6 font doubled
    text = f"{int(done * 100):3d}%"
    small = np.zeros((CHAR_H, text_width(text) + 1, 3), dtype=np.uint8)
    draw_text(small, 0, 0, text, COL_OK)
    big = np.repeat(np.repeat(small, 2, axis=0), 2, axis=1)
    x0 = max(0, (rw - big.shape[1]) // 2)
    right[3:3 + big.shape[0], x0:x0 + big.shape[1]] = big[:, :rw - x0]
    # hex chatter under it, changing a dozen times a second
    rng = random.Random(int(elapsed * 12))
    chatter = "".join(rng.choice("0123456789ABCDEF") for _ in range(12))
    draw_text(right, max(0, (rw - text_width(chatter)) // 2), 17, chatter, COL_DIM)
    # segmented bar along the bottom
    segments = 20
    seg_w = max(1, (rw - 4) // segments)
    for n in range(segments):
        xa = 2 + n * seg_w
        lit = n < int(done * segments + 0.5)
        right[26:30, xa:xa + seg_w - 1] = COL_CONSOLE if lit else (0, 40, 14)
    return np.concatenate([left, right], axis=1)


_TRACE_CACHE: dict = {}


def _face_parts(eye_mask, mouth_mask):
    """Face coverage 0..1 and its outline, cached per pair of masks."""
    key = (id(eye_mask), id(mouth_mask))
    if key not in _TRACE_CACHE:
        alpha = np.maximum(eye_mask, mouth_mask).astype(np.float32) / 255.0
        solid = alpha > 0.35
        pad = np.pad(solid, 1)
        inner = pad[:-2, 1:-1] & pad[2:, 1:-1] & pad[1:-1, :-2] & pad[1:-1, 2:]
        edge = solid & ~inner
        ys, xs = np.nonzero(eye_mask[:, :config.W // 2] > 90)
        centre = (float(xs.mean()), float(ys.mean())) if len(xs) else (config.W / 4.0, config.H / 2.0)
        _TRACE_CACHE[key] = (alpha, edge, centre)
    return _TRACE_CACHE[key]


def render_trace(eye_mask, mouth_mask, p: float, t: float) -> np.ndarray:
    """A beam draws the face: out from the middle it traces the outline,
    on the way back it fills the shapes with glowing metal that cools to red."""
    W, H = config.W, config.H
    alpha, edge, _ = _face_parts(eye_mask, mouth_mask)
    img = np.zeros((H, W, 3), dtype=np.float32)
    xs = np.arange(W, dtype=np.float32)[None, :]
    rng = random.Random(int(t * 30))
    if p < 0.45:                                   # pass 1: the outline, from the nose outwards
        beam = (W - 1) * (1.0 - _ease_out(p / 0.45))
        drawn = (xs >= beam) & edge
        age = np.clip((xs - beam) / 10.0, 0.0, 1.0)            # fresh lines are brighter
        for c in range(3):
            img[:, :, c] = drawn * (COL_TRACE[c] * (0.55 + 0.45 * (1.0 - age)))
    else:                                          # pass 2: the fill, coming back
        q = min(1.0, (p - 0.45) / 0.45)
        beam = (W - 1) * _ease_out(q)
        filled = xs <= beam
        heat = np.exp(-np.clip(beam - xs, 0.0, None) / 5.0) * filled   # white-hot right behind the beam
        outline_left = edge & ~filled
        for c in range(3):
            cool = COL_FACE[c]
            img[:, :, c] = alpha * filled * (cool + (255.0 - cool) * heat) + outline_left * COL_TRACE[c] * 0.6
    if p < 0.9:                                    # the beam itself, and sparks where it cuts
        bx = int(round(max(0.0, min(W - 1.0, beam))))
        img[:, bx, :] = np.maximum(img[:, bx, :], np.array(COL_BEAM, dtype=np.float32) * 0.16)
        hits = np.nonzero(alpha[:, bx] > 0.2)[0]
        for y in hits:
            img[y, bx] = COL_BEAM
        for _ in range(min(7, 2 * len(hits))):
            y = rng.choice(hits.tolist()) + rng.randint(-3, 3)
            x = bx + rng.randint(-5, 5)
            if 0 <= y < H and 0 <= x < W:
                img[y, x] = COL_SPARK
    return _mirrored(np.clip(img, 0, 255).astype(np.uint8))


def render_ignite(eye_mask, mouth_mask, p: float, t: float) -> np.ndarray:
    """The face catches: a flash, a shock ring out of the eye, white cooling
    to its colour, the picture twitching for a moment."""
    W, H = config.W, config.H
    alpha, _, (cx, cy) = _face_parts(eye_mask, mouth_mask)
    if p < 0.5:                                    # white -> orange -> face colour
        k = p / 0.5
        col = (255.0, 255.0 * (1 - k) + COL_PULSE[1] * k, 200.0 * (1 - k) + COL_PULSE[2] * k)
    else:
        k = (p - 0.5) / 0.5
        col = tuple(COL_PULSE[c] * (1 - k) + COL_FACE[c] * k for c in range(3))
    img = alpha[:, :, None] * np.array(col, dtype=np.float32)[None, None, :]
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    dist = np.hypot(xx - cx, (yy - cy) * 1.15)
    for delay, strength in ((0.0, 1.0), (0.18, 0.55)):         # two rings, the second weaker
        q = (p - delay) / (1.0 - delay)
        if 0.0 < q < 1.0:
            ring = np.exp(-((dist - q * 46.0) / 1.6) ** 2) * (1.0 - q) ** 1.5 * strength
            img += ring[:, :, None] * np.array((255.0, 190.0, 110.0), dtype=np.float32)
    if p < 0.14:                                   # the flash
        img += (1.0 - p / 0.14) * 150.0
    out = np.clip(img, 0, 255).astype(np.uint8)
    if p < 0.45:                                   # a few rows jump sideways
        rng = random.Random(int(t * 30))
        for _ in range(3):
            y = rng.randrange(H)
            out[y] = np.roll(out[y], rng.randint(-4, 4), axis=0)
    return _mirrored(out)


# ==========================================================================
# Sequence controller
# ==========================================================================
class BootSequence:
    """power-on -> boot log -> rain -> the beam draws the face -> it ignites.

    render() returns a frame of one panel's width or of the whole chain; the
    renderer mirrors only the narrow ones."""
    CONSOLE_TOTAL = len(BOOT_LINES) * CONSOLE_LINE_TIME
    TOTAL = POWER_DURATION + CONSOLE_TOTAL + RAIN_V3_DURATION + TRACE_DURATION + IGNITE_DURATION

    def __init__(self):
        self.start = time.monotonic()
        self.done  = False
        self.stage = STAGE_POWER
        # reset rain state so each boot starts fresh
        global _RAIN
        _RAIN = _RainState()

    def elapsed(self) -> float:
        return time.monotonic() - self.start

    def current_stage(self) -> str:
        return self.stage

    def render(self, eye_mask: np.ndarray, mouth_mask: np.ndarray):
        e = self.elapsed()

        if e < POWER_DURATION:
            self.stage = STAGE_POWER
            return render_power_on(e / POWER_DURATION)
        e -= POWER_DURATION

        if e < self.CONSOLE_TOTAL:
            self.stage = STAGE_CONSOLE
            return render_console_wide(e, self.CONSOLE_TOTAL)
        e -= self.CONSOLE_TOTAL

        if e < RAIN_V3_DURATION:
            self.stage = STAGE_RAIN
            blend_out = max(0.0, (e - (RAIN_V3_DURATION - 0.3)) / 0.3)
            return _mirrored(render_rain(e, blend_out))
        e -= RAIN_V3_DURATION

        if e < TRACE_DURATION:
            self.stage = STAGE_TRACE
            return render_trace(eye_mask, mouth_mask, e / TRACE_DURATION, e)
        e -= TRACE_DURATION

        if e < IGNITE_DURATION:
            self.stage = STAGE_IGNITE
            return render_ignite(eye_mask, mouth_mask, e / IGNITE_DURATION, e)

        self.stage = STAGE_DONE
        self.done  = True
        return None


# kept for backward compatibility
def breathing_offset(t: float):
    dx = round(math.sin(t * 0.7) * 0.8)
    dy = round(math.sin(t * 0.5 + 1.2) * 0.6 + math.sin(t * 0.21) * 0.3)
    return int(dx), int(dy)
