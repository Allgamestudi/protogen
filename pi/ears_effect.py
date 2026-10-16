"""Effect engine for the WS2812 ear rings.

Runs in its own thread at ~30 Hz, computes per-LED colours based on the
current effect mode, pushes via `ears.push()`.

Effects:
    off       - all LEDs dark, no packets sent
    solid     - every LED is one colour (from `color`)
    rainbow   - HSV rotates around each ring
    spectrum  - 13 LEDs = 13 FFT bands, brightness = magnitude, hue = `color`
    pulse     - solid `color`, brightness pulsates with audio RMS / beat
    face      - colour follows the current matrix face colour (state['color'])
    wheel     - slow chase: one bright dot rotates around the ring
"""
import colorsys
import math
import threading
import time

import ears
from state import state, lock, target_bars, stop_event, log_err


# module state
_effect       = "face"         # default after boot - matches face colour
_color        = (255, 0, 64)   # picker colour, used by colour-based effects
_brightness   = 0.8            # multiplier 0..1, applied to final RGB
_running      = False
_thread       = None

# Wait this many seconds after start() before the runner begins pushing -
# gives the ESP's own boot animation (self-test + spinner + ready flash, ~3 s)
# time to finish without our packets piling up in the USB buffer.
_BOOT_DELAY_S = 4.0

VALID_EFFECTS = ("off", "solid", "rainbow", "spectrum", "pulse", "face", "wheel")


# helpers
def _scaled(rgb):
    """Apply brightness multiplier."""
    return (int(rgb[0] * _brightness),
            int(rgb[1] * _brightness),
            int(rgb[2] * _brightness))


def _hsv_rgb(h, s, v):
    """0..1 HSV -> 0..255 RGB tuple."""
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return (int(r * 255), int(g * 255), int(b * 255))


def _color_to_hue(rgb):
    """RGB -> hue (0..1) for use as hue base in spectrum effect."""
    r, g, b = rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0
    h, _, _ = colorsys.rgb_to_hsv(r, g, b)
    return h


# effect renderers
def _render_off():
    ears.clear()


def _render_solid():
    ears.set_all(_scaled(_color))


def _render_rainbow(t):
    speed = 0.3
    out_l, out_r = [], []
    for i in range(ears.EAR_COUNT):
        h = (i / ears.EAR_COUNT + t * speed) % 1.0
        rgb = _hsv_rgb(h, 1.0, 1.0)
        out_l.append(_scaled(rgb))
        out_r.append(_scaled(rgb))
    ears.set_left(out_l); ears.set_right(out_r)


def _render_spectrum():
    """Each LED = magnitude of FFT band; hue from picked color."""
    hue = _color_to_hue(_color)
    bars = list(target_bars[:ears.EAR_COUNT]) if len(target_bars) else [0.0] * ears.EAR_COUNT
    # Pad / trim to 13 LEDs
    while len(bars) < ears.EAR_COUNT:
        bars.append(0.0)
    out_l, out_r = [], []
    for i in range(ears.EAR_COUNT):
        v = max(0.0, min(1.0, float(bars[i])))
        # tint colour at band-proportional saturation, value scales by magnitude
        rgb = _hsv_rgb(hue, 1.0, v)
        out_l.append(_scaled(rgb))
        out_r.append(_scaled(rgb))
    ears.set_left(out_l); ears.set_right(out_r)


def _render_pulse():
    """Solid colour, brightness pulses with audio energy."""
    bars = list(target_bars[:ears.EAR_COUNT]) if len(target_bars) else []
    energy = sum(bars) / max(1, len(bars))
    pulse = 0.2 + 0.8 * min(1.0, energy * 1.5)
    r, g, b = _color
    rgb = (int(r * pulse * _brightness),
           int(g * pulse * _brightness),
           int(b * pulse * _brightness))
    ears.set_all(rgb)


def _render_face():
    """Match the current matrix face colour."""
    with lock:
        col = state.get("color", [255, 30, 30])
    ears.set_all(_scaled((col[0], col[1], col[2])))


def _render_wheel(t):
    """Single bright dot rotates around each ring with a fading trail."""
    head = int((t * 4.0) % ears.EAR_COUNT)         # ~4 steps/sec
    trail = 4
    out_l, out_r = [], []
    for i in range(ears.EAR_COUNT):
        dist = (head - i + ears.EAR_COUNT) % ears.EAR_COUNT
        v = (255 >> (dist + 1)) if dist < trail else 0
        rgb = (int(_color[0] * v / 255),
               int(_color[1] * v / 255),
               int(_color[2] * v / 255))
        out_l.append(_scaled(rgb))
        # right ear spins opposite for symmetry
        out_r.append((0, 0, 0))
    for i in range(ears.EAR_COUNT):
        dist = ((ears.EAR_COUNT - 1 - head) - i + ears.EAR_COUNT) % ears.EAR_COUNT
        v = (255 >> (dist + 1)) if dist < trail else 0
        rgb = (int(_color[0] * v / 255),
               int(_color[1] * v / 255),
               int(_color[2] * v / 255))
        out_r[i] = _scaled(rgb)
    ears.set_left(out_l); ears.set_right(out_r)


# main loop
_FPS = 30                                # match the matrix renderer
_DT = 1.0 / _FPS


def _run():
    # Hold off pushing until the ESP finishes its own boot animation
    boot_end = time.monotonic() + _BOOT_DELAY_S
    while not stop_event.is_set() and time.monotonic() < boot_end:
        time.sleep(0.1)
    t0 = time.monotonic()
    while not stop_event.is_set():
        t = time.monotonic() - t0
        try:
            eff = _effect
            if   eff == "off":
                pass                            # no push - keep last state / dark
            elif eff == "solid":    _render_solid();    ears.push()
            elif eff == "rainbow":  _render_rainbow(t); ears.push()
            elif eff == "spectrum": _render_spectrum(); ears.push()
            elif eff == "pulse":    _render_pulse();    ears.push()
            elif eff == "face":     _render_face();     ears.push()
            elif eff == "wheel":    _render_wheel(t);   ears.push()
        except Exception as e:
            log_err(f"ears effect '{_effect}': {e}")
        time.sleep(_DT)


# public api
def set_effect(name):
    """Switch active effect. If 'off' - sends a clear once, then stops pushing."""
    global _effect
    if name not in VALID_EFFECTS:
        return False
    was = _effect
    _effect = name
    if name == "off" and was != "off":
        ears.clear()
        ears.push()
    return True


def set_color(rgb):
    global _color
    _color = (int(rgb[0]), int(rgb[1]), int(rgb[2]))


def set_brightness(b):
    global _brightness
    _brightness = max(0.0, min(1.0, float(b)))


def get_state():
    return {
        "effect":     _effect,
        "color":      list(_color),
        "brightness": _brightness,
        "available":  list(VALID_EFFECTS),
    }


def start():
    global _running, _thread
    if _running:
        return
    _running = True
    _thread = threading.Thread(target=_run, daemon=True, name="ears-effect")
    _thread.start()
