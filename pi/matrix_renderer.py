"""Matrix render thread - animation engine v2.

Per-frame pipeline (emotion mode):
  AudioFeatures -> Behavior FSM -> vignette overrides
  -> polygon face (emotion morph + lip-sync + blink profiles + saccades)
  -> colorize (solid / gradient / rainbow / audio) over background (party bars)
  -> aura outline, eye sparkles, beat flash
  -> single affine transform (breathing shift+scale, head tilt, beat zoom/shake)
  -> sleep overlay (Zzz) / ERROR overlay
  -> post-fx chain (scanline, noise, rgb-split, glitch, bloom)

Other modes (image/gif, text, eq/spectrogram) kept from v1.
"""
import colorsys
from functools import lru_cache
import math
import random
import threading
import time
import traceback

import numpy as np
from PIL import Image
from rgbmatrix import RGBMatrix, RGBMatrixOptions

import config
import emotions
import boot_animation
import animations
import postfx
import dino_game
import liveliness
import reactions
import state as shared
from state import (
    state, lock, stop_event, log_err,
    target_bars, uploaded_image, uploaded_gif_frames,
)
import colormusic

# Emotion pairs that use a glitch-cut transition instead of a plain morph
CONTRAST_PAIRS = {
    frozenset(("happy", "angry")),
    frozenset(("love", "angry")),
    frozenset(("sleepy", "surprised")),
    frozenset(("sad", "happy")),
}

ERROR_GRACE_S = 15.0     # no ERROR screen during the first seconds after start
ERROR_AFTER_S = 6.0      # ESP silent for this long -> ERROR state


# motion helpers
def _breathing_offset(t, amp_x, amp_y, speed):
    t = t * speed
    dx = (math.sin(t * 0.62) * 0.55 + math.sin(t * 0.23 + 1.7) * 0.30) * amp_x
    dy = (math.sin(t * 0.41 + 1.2) * 0.45 +
          math.sin(t * 0.17 + 0.4) * 0.35 +
          math.sin(t * 0.09 + 3.1) * 0.20) * amp_y
    return dx, dy


def _breathing_scale(t, speed=1.0):
    """Asymmetric breath cycle: inhale 1.8 s -> pause -> exhale 2.6 s -> pause."""
    cyc = (t * speed) % 5.6
    if cyc < 1.8:
        return 0.5 - 0.5 * math.cos(math.pi * (cyc / 1.8))
    if cyc < 2.2:
        return 1.0
    if cyc < 4.8:
        return 0.5 + 0.5 * math.cos(math.pi * ((cyc - 2.2) / 2.6))
    return 0.0


def _build_affine(zoom, angle_deg, dx, dy, cx, cy):
    """Inverse-mapping coefficients for PIL Image.transform AFFINE:
    forward = translate(dx,dy) o rotate(angle) o scale(zoom) about (cx,cy)."""
    th = math.radians(angle_deg)
    cos_t, sin_t = math.cos(th), math.sin(th)
    a = cos_t / zoom
    b = sin_t / zoom
    d = -sin_t / zoom
    e = cos_t / zoom
    c = cx - a * (cx + dx) - b * (cy + dy)
    f = cy - d * (cx + dx) - e * (cy + dy)
    return (a, b, c, d, e, f)


# color helpers
def _make_color_grad():
    grad = np.zeros((config.H, 3), dtype=np.uint8)
    for y in range(config.H):
        yn = (config.H - 1 - y) / (config.H - 1)
        if yn < 0.5:
            grad[y] = (int(yn * 2 * 255), 255, 0)
        else:
            grad[y] = (255, int((1 - (yn - 0.5) * 2) * 255), 0)
    return grad


def _make_gradient_map(colors, angle_deg):
    return _cached_gradient_map(
        tuple(tuple(color) for color in colors), angle_deg, config.H, config.W)


@lru_cache(maxsize=1)
def _cached_gradient_map(colors, angle_deg, H, W):
    """Keep only the current gradient; shared maps must not be modified."""
    if len(colors) == 1:
        c = np.array(colors[0], dtype=np.float32)
        return np.broadcast_to(c, (H, W, 3))
    rad = math.radians(angle_deg)
    cx, cy = math.cos(rad), math.sin(rad)
    xs = np.linspace(-0.5, 0.5, W, dtype=np.float32)
    ys = np.linspace(-0.5, 0.5, H, dtype=np.float32)
    xx, yy = np.meshgrid(xs, ys)
    t = xx * cx + yy * cy
    t = (t - t.min()) / (t.max() - t.min() + 1e-9)
    n = len(colors)
    stops = np.array(colors, dtype=np.float32)
    t_seg = t * (n - 1)
    idx = np.clip(t_seg.astype(np.int32), 0, n - 2)
    frac = (t_seg - idx)[..., np.newaxis]
    c0 = stops[idx]
    c1 = stops[np.minimum(idx + 1, n - 1)]
    result = np.clip(c0 * (1 - frac) + c1 * frac, 0, 255).astype(np.float32)
    result.setflags(write=False)
    return result


@lru_cache(maxsize=1)
def _rainbow_palette():
    """Match Pillow's 8-bit HSV conversion once for all 256 hue values."""
    hsv = np.full((1, 256, 3), 255, dtype=np.uint8)
    hsv[0, :, 0] = np.arange(256, dtype=np.uint8)
    result = np.asarray(
        Image.frombytes("HSV", (256, 1), hsv.tobytes()).convert("RGB"),
        dtype=np.float32,
    )[0]
    result.setflags(write=False)
    return result


def _make_rainbow_map(diag, offset):
    hue = ((diag * 16 - offset) % 256).astype(np.uint8)
    return _rainbow_palette()[hue]


def _audio_color(centroid, rms_slow, hue_kick=0.0):
    """Spectral centroid -> hue; long-term loudness -> saturation."""
    hue = (max(0.0, min(1.0, centroid)) * 0.75 + hue_kick) % 1.0
    sat = max(0.55, min(1.0, 0.55 + rms_slow * 2.0))
    r, g, b = colorsys.hsv_to_rgb(hue, sat, 1.0)
    return np.array([r * 255, g * 255, b * 255], dtype=np.float32)


def flash_falloff(h, w):
    """How much of a flash each pixel of the dark background gets: 1 in the middle
    of the panel, 0 at its border, so the flash never draws the panel's rectangle."""
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    d = np.hypot((x - (w - 1) / 2.0) / (w / 2.0), (y - (h - 1) / 2.0) / (h / 2.0))
    return np.clip(1.0 - d, 0.0, 1.0) ** 1.5


def add_flash(frame, flash, falloff, strength=200.0):
    """Brighten the frame for a flash. The drawn face gets all of it; the empty
    background only as much as `falloff` allows."""
    lit = frame.max(axis=2) > 24
    amount = np.where(lit, 1.0, falloff) * (flash * strength)
    return np.clip(frame.astype(np.float32) + amount[..., None], 0, 255).astype(np.uint8)


def _party_background(bars, now):
    """Bottom spectrum bars with rotating hues (PARTY mode backdrop)."""
    frame = np.zeros((config.H, config.W, 3), dtype=np.float32)
    n = bars.size
    if n == 0:
        return frame
    bw = max(1, config.W // n)
    for i in range(n):
        h = int(bars[i] * 10)
        if h <= 0:
            continue
        hue = (i / n + now * 0.08) % 1.0
        r, g, b = colorsys.hsv_to_rgb(hue, 1.0, 0.55)
        frame[config.H - h:, i * bw:(i + 1) * bw] = (r * 255, g * 255, b * 255)
    return frame


# misc helpers
def _shift_x(img, s, reflect=False):
    """Move a float image right by s pixels (left if negative), empty behind it.

    reflect: what comes in over the right edge is the mirror image of the
    picture. That edge is the middle of the face, where the two panels meet,
    and the other half continues there (the mouth must not get a gap).
    """
    k = int(math.floor(s))
    f = s - k
    w = img.shape[1]

    def moved(n):
        out = np.zeros_like(img)
        if n >= w or n <= -w:
            return out
        if n >= 0:
            out[:, n:] = img[:, :w - n]
        else:
            out[:, :w + n] = img[:, -n:]
            if reflect:
                out[:, w + n:] = img[:, :w + n - 1:-1]
        return out

    return moved(k) if f < 1e-3 else moved(k) * (1.0 - f) + moved(k + 1) * f


def _squash_rows(img, centre, scale, lift):
    """Squeeze a float image vertically towards row `centre` (scale 1 = as is)
    and raise it by `lift` pixels; empty where nothing maps to."""
    h = img.shape[0]
    src = centre + (np.arange(h, dtype=np.float32) + lift - centre) / max(scale, 0.08)
    lo = np.floor(src).astype(int)
    frac = (src - lo)[:, None, None]
    padded = np.concatenate([img, np.zeros_like(img[:1])], axis=0)      # row h = empty
    a = padded[np.where((lo >= 0) & (lo < h), lo, h)]
    b = padded[np.where((lo + 1 >= 0) & (lo + 1 < h), lo + 1, h)]
    return a * (1.0 - frac) + b * frac


def _halo_on_panel(halo, coeffs):
    """Masks after the same affine transform the picture went through.
    One mask (H x W) gives H x W x 1; three stacked (H x W x 3) stay stacked."""
    out = np.asarray(
        Image.fromarray((halo * 255.0).astype(np.uint8)).transform(
            (config.W, config.H), Image.AFFINE, coeffs, Image.BILINEAR),
        dtype=np.float32) / 255.0
    return out[:, :, None] if out.ndim == 2 else out


def _eye_variant(frame, mask, shift=0.0, closure=0.0, lift=0.0, sway=0.0, pupil=None):
    """The same picture with the eye changed: moved sideways by `shift`, shut
    by `closure` (0..1), raised by `lift`; then all of it moved by `sway`.
    pupil = (where the pupil is, where it should be), two masks: the pupil is
    moved inside the eye (filled with the colour of the eye in that row,
    darkened in the new place).

    The second panel is a mirror image of the first, so an eye that looks left
    on one looks right on the other and the gaze falls apart, and a wink would
    always be both eyes. The eye is cut out with its glow and highlight (mask)
    and put back changed, so each panel can have its own.
    """
    f = frame.astype(np.float32)
    # Only what stands out from the background is the eye. On a lit background
    # (beat flash, party bars) cutting the whole patch would leave a dark hole.
    ring = (mask[:, :, 0] > 0.02) & (mask[:, :, 0] < 0.5)
    around = np.median(f[ring], axis=0) if ring.any() else 0.0
    eye = np.maximum(f - around, 0.0) * mask
    f = f - eye
    if pupil is not None:
        old, new = pupil
        level = eye.max(axis=2)
        lit = level > np.maximum(0.85 * level.max(axis=1, keepdims=True), 8.0)    # the full-bright part of the row
        count = lit.sum(axis=1)
        row_colour = (eye * lit[:, :, None]).sum(axis=1) / np.maximum(count, 1)[:, None]
        eye = eye * (1.0 - old) + row_colour[:, None, :] * old
        eye = eye * (1.0 - new)
    if closure > 0.01 or abs(lift) > 0.02:
        rows = mask[:, :, 0].sum(axis=1)
        centre = float((rows * np.arange(len(rows))).sum() / rows.sum()) if rows.sum() > 0 else 0.0
        eye = _squash_rows(eye, centre, 1.0 - 0.9 * min(1.0, closure), lift)
    f = f + (_shift_x(eye, shift) if abs(shift) > 0.02 else eye)
    if abs(sway) > 0.02:
        f = _shift_x(f, sway, reflect=True)
    return np.clip(f, 0, 255).astype(np.uint8)


def _limit_power(frame, brightness, limit):
    """Dim a frame that would draw more current than the supply can give.

    A face lights a few percent of the LEDs; a photo or a white screen lights
    all of them and takes many times the current. The supply voltage then
    sags and the Raspberry Pi reboots. The load of a frame is its mean level
    times the panel brightness: 1.0 = everything white at 100 %. Frames above
    `limit` are scaled down to it; darker ones pass untouched.
    Returns (frame, load before limiting).
    """
    load = float(frame.mean()) / 255.0 * (brightness / 100.0)
    if limit >= 1.0 or load <= limit or load <= 0.0:
        return frame, load
    return (frame.astype(np.float32) * (limit / load)).astype(np.uint8), load


def _compose_dual(frame):
    """Tile into (H x W*2): left = original (panel 0), right = mirrored (panel 1).

    Physical chain runs right->left from the Pi, so panel 0 (first in chain,
    closest to the GPIO header) is the RIGHT half of the artist's wide face;
    we leave it as drawn and mirror the LEFT half onto panel 1.
    """
    mirrored = frame[:, ::-1, :]
    return np.concatenate([frame, mirrored], axis=1)


def _render_text(text, offset_x, color, full_w, scale=1, effect="solid", t=0.0):
    """Render scrolling text across the full chained panel width.

    full_w   - target frame width (128 if mirrored chain, 64 if single)
    scale    - pixel-multiplier (1 = 4x6 font, 2 = 8x12, 3 = 12x18)
    effect   - "solid" / "rainbow" / "blink" / "color_cycle"
    t        - current monotonic time, used by time-based effects
    """
    frame = np.zeros((config.H, full_w, 3), dtype=np.uint8)
    if not text:
        return frame
    text = text.upper()
    text_w_raw = boot_animation.text_width(text)
    text_w = text_w_raw * scale
    char_h = boot_animation.CHAR_H * scale
    y = max(0, (config.H - char_h) // 2)
    gap = full_w + 6 * scale
    loop_w = text_w + gap
    x = full_w - int(offset_x) % loop_w

    # Pick the effective colour for this frame (effects modulate it)
    eff_color = color
    if effect == "blink":
        eff_color = color if (int(t * 2) & 1) else (0, 0, 0)
    elif effect == "color_cycle":
        h = (t * 0.3) % 1.0
        r, g, b = colorsys.hsv_to_rgb(h, 1.0, 1.0)
        eff_color = (int(r * 255), int(g * 255), int(b * 255))

    if scale == 1:
        # Fast path - draw straight into the frame
        boot_animation.draw_text(frame, x,          y, text, eff_color)
        boot_animation.draw_text(frame, x + loop_w, y, text, eff_color)
    else:
        # Render text to a small 1x buffer then upscale by `scale` via PIL
        tmp = np.zeros((boot_animation.CHAR_H, text_w_raw + 1, 3), dtype=np.uint8)
        boot_animation.draw_text(tmp, 0, 0, text, eff_color)
        from PIL import Image as _Im
        big = _Im.fromarray(tmp).resize(
            (text_w_raw * scale, boot_animation.CHAR_H * scale), _Im.NEAREST)
        big_np = np.asarray(big)

        def _paste(dst_x):
            x0 = max(0, dst_x); x1 = min(full_w, dst_x + big_np.shape[1])
            sx0 = x0 - dst_x; sx1 = sx0 + (x1 - x0)
            if x1 > x0 and y + char_h <= config.H:
                frame[y:y + char_h, x0:x1] = big_np[:, sx0:sx1]

        _paste(x)
        _paste(x + loop_w)

    if effect == "rainbow":
        # Every lit pixel in a column has the same hue.
        mask = frame.any(axis=2)
        ys, xs = np.where(mask)
        columns = np.flatnonzero(mask.any(axis=0))
        column_colors = np.zeros((full_w, 3), dtype=np.uint8)
        for px in columns:
            h = ((px / full_w) + t * 0.3) % 1.0
            r, g, b = colorsys.hsv_to_rgb(h, 1.0, 1.0)
            column_colors[px] = (int(r * 255), int(g * 255), int(b * 255))
        frame[ys, xs] = column_colors[xs]

    return frame


def _render_spectrogram(hist, gain=1.4):
    H, W = config.H, config.W
    frame = np.zeros((H, W, 3), dtype=np.uint8)
    n_frames, n_bars = hist.shape
    ys = (np.arange(H) * n_frames // H)
    xs = (np.arange(W) * n_bars // W)
    v = np.clip(hist[ys][:, xs] * gain, 0, 1)
    lo = v < 0.33
    mid = (v >= 0.33) & (v < 0.66)
    hi = v >= 0.66
    frame[..., 1][lo] = (v[lo] * 3 * 255).astype(np.uint8)
    frame[..., 0][mid] = ((v[mid] - 0.33) * 3 * 255).astype(np.uint8)
    frame[..., 1][mid] = 255
    frame[..., 0][hi] = 255
    frame[..., 1][hi] = (255 - (v[hi] - 0.66) * 3 * 255).astype(np.uint8)
    return frame


_eye_coords_cache = {}


def _eye_coords(emotion):
    if emotion not in _eye_coords_cache:
        lay = emotions.get_layout(emotion)
        if lay is not None:                      # the eye alone, without the nose
            _eye_coords_cache[emotion] = lay.eye_pixels
        else:
            m = emotions.get_eye_mask(emotion)
            ys, xs = np.nonzero(m)
            _eye_coords_cache[emotion] = list(zip(xs.tolist(), ys.tolist()))
    return _eye_coords_cache[emotion]


def _build_matrix():
    o = RGBMatrixOptions()
    o.rows = config.H
    o.cols = config.W
    o.chain_length = config.CHAIN_LENGTH
    o.hardware_mapping = config.HW_MAPPING
    o.gpio_slowdown = config.GPIO_SLOWDOWN
    o.brightness = config.BRIGHTNESS
    o.pwm_bits = config.PWM_BITS
    # CRITICAL: keep root after GPIO init so settings.json / ui_state.json can
    # be written to /home/wild/protogen/saves/.  Without this the rpi-rgb-led-
    # matrix library silently drops to user `daemon`, breaking every persist
    # path we have.
    o.drop_privileges = False
    return RGBMatrix(options=o)


def _run():
    try:
        _render_loop()
    except Exception:
        print("=== MATRIX_LOOP FAILED ===")
        traceback.print_exc()


# main loop
def _render_loop():
    matrix = _build_matrix()
    canvas = matrix.CreateFrameCanvas()

    color_grad = _make_color_grad()
    bands = config.BANDS
    bar_w = config.W // bands
    gap = 1

    display = np.zeros(bands, dtype=np.float32)
    peaks = np.zeros(bands, dtype=np.float32)
    peak_v = np.zeros(bands, dtype=np.float32)
    mouth_open_level = 0.0

    yy, xx = np.meshgrid(np.arange(config.H), np.arange(config.W), indexing="ij")
    diag = (xx + yy).astype(np.int32)

    # animators / engine objects
    feat_ex = animations.FeatureExtractor()
    blinker = animations.Blinker()
    sac = animations.Saccade()
    behavior = animations.Behavior(sac)
    spec = animations.SpectrogramHistory(n_bars=bands, n_frames=config.H)
    tilt_lfo = animations.LFO(freq=1.0 / 7.0, amp=1.0)

    env_zoom = animations.Envelope(decay=8.0)
    env_shake = animations.Envelope(decay=10.0)
    env_flash = animations.Envelope(decay=6.0)
    flash_mask = flash_falloff(config.H, config.W)
    env_mouth = animations.Envelope(decay=7.0)
    env_live = animations.Envelope(decay=9.0)
    arrival_pending = False                # a change of emotion is under way
    agc = reactions.AutoGain(bands)
    visemes = reactions.Visemes()
    auto_emotion = reactions.AutoEmotion()
    gestures = reactions.Gestures()
    asym = reactions.Asymmetry()
    env_glitch = animations.Envelope(decay=7.0)
    env_rgbsplit = animations.Envelope(decay=9.0)
    env_hue = animations.Envelope(decay=4.0)
    env_tilt = animations.Envelope(decay=2.5)
    tilt_sign = 1.0
    # IMU vestibulo-ocular eye drift (eyes lag head tilt with spring physics)
    eye_off_x = 0.0; eye_off_vx = 0.0      # eye horizontal offset (px) + velocity
    eye_off_y = 0.0; eye_off_vy = 0.0
    # IMU step bob (vertical Y offset, spring kicked by detected step impulses)
    bob_y = 0.0;     bob_vy = 0.0          # face vertical offset (px) + velocity
    az_lp = 1.0                            # low-pass of az in g (gravity baseline)
    last_step_t = 0.0
    # Dizzy effect - accumulated shake -> spinning spirals over eyes + open mouth
    shake_accum_t   = 0.0                  # seconds of continuous shake
    dizzy_remaining = 0.0                  # countdown of active dizzy state
    dizzy_phase     = 0.0                  # rotating phase of spiral, radians
    shake_ema       = 0.0                  # EMA of |mag(a)|-1 g, drives Pi-side shake detection
    # Impact OUCH effect - brief startled override on bonk
    impact_timer    = 0.0                  # seconds remaining of OUCH override
    # Proximity reactions - track last zone for transition triggers
    prev_prox_zone  = "unknown"
    prox_recoil_dy  = 0.0                  # vertical recoil offset, springs back
    prox_recoil_vy  = 0.0                  # velocity of recoil spring
    prox_eye_scale  = 1.0                  # eye scale modifier, springs to target

    sparkles = []          # [x, y, frames_left]
    zzz = []               # {"x","y","t0"}
    last_zzz_t = 0.0
    loud_since = None
    was_long_silence = False

    # proximity state - smoothed distance + last sample for velocity calc
    prox_smoothed = None
    prox_last_mm = None
    prox_last_t = 0.0
    last_gesture_t = 0.0

    gif_idx = 0
    gif_t = 0.0
    frame_no = 0

    last = time.monotonic()
    t0 = last
    last_conn_t = t0 + ERROR_GRACE_S
    frame_dt = 1.0 / config.TARGET_FPS

    boot = boot_animation.BootSequence()
    # Trigger the ESP ear-ring boot animation at the same instant the matrix
    # boot sequence kicks off - keeps both visuals synchronised.
    # Retry for ~3 s because the serial reader thread may still be opening
    # /dev/ttyACM0 when the renderer first reaches this line.
    try:
        from serial_proto import send_command
        from state import ser_handle
        def _trigger_ears():
            for _ in range(30):
                if ser_handle.get("ser") is not None:
                    send_command("B")
                    return
                time.sleep(0.1)
            log_err("ears boot: serial never connected")
        threading.Thread(target=_trigger_ears, daemon=True,
                         name="ears-boot-trigger").start()
    except Exception as e:
        log_err(f"ears boot trigger: {e}")
    eye_mask = emotions.get_eye_mask("angry")
    mouth_mask = emotions.get_mouth_mask("angry")

    _emo_from = "angry"
    _emo_to = "angry"
    _emo_blend = 1.0
    # Dino game - ToF jump trigger state (edge detector with hysteresis).
    # Thresholds live in cfg (dino_tof_lo_mm / dino_tof_hi_mm) - adjust via UI.
    _dino_tof_armed = True

    while not stop_event.is_set():
        now = time.monotonic()
        dt = now - last
        last = now
        frame_no += 1
        frame_b = None          # own picture for the mirrored panel, when it differs

        with lock:
            mode = state["mode"]
            br = state["brightness"]
            rot = state["rotate_180"]
            mir = state["mirror"]
            emotion = state["emotion"]
            effect = state["effect"]
            color = list(state["color"])
            grad_colors = list(state.get("gradient_colors", [[255, 0, 0], [0, 0, 255]]))
            grad_angle = float(state.get("gradient_angle", 0))
            text_content = state.get("text_content", "")
            text_color = list(state.get("text_color", [0, 220, 70]))
            party = bool(state.get("party", False))
            connected = bool(state.get("connected", False))
            cfg = dict(state["settings"])
        if getattr(emotions, "set_eye_style", lambda s: False)(cfg.get("eye_style", 0)):
            _eye_coords_cache.clear()

        if matrix.brightness != br:
            matrix.brightness = br

        # BOOT
        if not boot.done:
            frame = boot.render(eye_mask, mouth_mask)
            if frame is None:
                continue
            img = Image.fromarray(frame)
            if rot:
                img = img.transpose(Image.ROTATE_180)
            if mir and config.CHAIN_LENGTH >= 2 and img.width < config.W * config.CHAIN_LENGTH:
                img = Image.fromarray(_compose_dual(np.array(img)))     # wide boot frames come ready
            img = Image.fromarray(_limit_power(np.asarray(img), br, cfg.get("power_limit", 0.25))[0])
            shared.preview["frame"], shared.preview["seq"] = np.asarray(img), shared.preview["seq"] - 1
            canvas.SetImage(img)
            canvas = matrix.SwapOnVSync(canvas)
            el = time.monotonic() - now
            if el < frame_dt:
                time.sleep(frame_dt - el)
            continue

        # audio features + behavior (shared by all modes)
        with lock:
            bars_now = target_bars.copy()
            raw_dist = state.get("distance_mm")
            dist_valid = state.get("distance_valid", False)
            last_dist_ms = state.get("last_distance_ms", 0)
        raw_rms = float(bars_now.mean())
        if cfg.get("auto_gain", True):
            bars_now = agc.process(bars_now, dt, min_peak=cfg.get("auto_gain_min_peak", 0.06))
        feat = feat_ex.update(bars_now, now, beat_threshold=cfg.get("beat_threshold", 1.5))

        # proximity (VL53L0X)
        prox_zone = "unknown"
        approach_speed = 0.0
        idle_speedup = 1.0
        proximity_enabled = bool(cfg.get("proximity_enabled", True))
        # Crosstalk dead-zone: VL53L1X through a visor reflects internally, giving
        # a constant "phantom" reading at ~20-50 mm. Treat anything closer than
        # this minimum as invalid noise.
        prox_min_mm = cfg.get("proximity_min_valid_mm", 50)
        if proximity_enabled and dist_valid and raw_dist is not None \
                and prox_min_mm < raw_dist < 4000:
            # consider stale > 1.5 s as unknown
            stale = (int(time.time() * 1000) - last_dist_ms) > 1500 if last_dist_ms else True
            if not stale:
                alpha = cfg.get("proximity_smoothing", 0.35)
                if prox_smoothed is None:
                    prox_smoothed = float(raw_dist)
                else:
                    prox_smoothed += (raw_dist - prox_smoothed) * alpha

                # velocity (positive = approaching, mm/s)
                if prox_last_mm is not None:
                    pdt = max(1e-3, now - prox_last_t)
                    approach_speed = (prox_last_mm - prox_smoothed) / pdt
                prox_last_mm = prox_smoothed
                prox_last_t = now

                near = cfg.get("proximity_near_mm", 100)
                close = cfg.get("proximity_close_mm", 400)
                far = cfg.get("proximity_far_mm", 1500)
                if prox_smoothed < near:
                    prox_zone = "near"
                elif prox_smoothed < close:
                    prox_zone = "close"
                elif prox_smoothed < far:
                    prox_zone = "mid"
                else:
                    prox_zone = "far"

                # gesture: rapid approach -> startled blink + shake
                gthr = cfg.get("proximity_gesture_mm_s", 800.0)
                if (approach_speed > gthr and now - last_gesture_t > 0.8):
                    last_gesture_t = now
                    blinker.trigger_startled()
                    env_shake.trigger(min(1.0, approach_speed / (gthr * 2.0)))
                    sac.react_to_sound(cfg.get("saccade_amp", 2.5))

                if prox_zone == "far":
                    idle_speedup = 1.0 / max(0.1, cfg.get("proximity_idle_factor", 0.4))
        else:
            prox_smoothed = None
            prox_last_mm = None

        with lock:
            state["distance_zone"] = prox_zone
            state["distance_smoothed_mm"] = (int(prox_smoothed)
                                              if prox_smoothed is not None else None)

        if connected:
            last_conn_t = now
        error_mode = (now - last_conn_t) > ERROR_AFTER_S

        # proximity can wake the protogen from SLEEP early
        if (cfg.get("proximity_wake_enabled", True)
                and prox_zone in ("near", "close")
                and behavior.state == "SLEEP"):
            behavior.state = "ACTIVE"
            behavior.vignette = None
            blinker.trigger_startled()
            env_shake.trigger(0.5)

        bstate = behavior.update(
            feat, now, party=party,
            idle_after=cfg.get("idle_after_s", 20.0) / idle_speedup,
            sleep_after=cfg.get("sleep_after_s", 150.0) / idle_speedup,
            vignettes_enabled=bool(cfg.get("vignettes_enabled", True)),
        )
        if behavior.prev_state == "SLEEP" and bstate == "ACTIVE":
            blinker.trigger_startled()
            env_shake.trigger(0.5)

        # near zone -> squint reflex; close zone -> brightness boost
        # Whole block wrapped in try/except so any bug here can never kill the
        # render thread (which would freeze the matrix on the last good frame).
        try:
            if proximity_enabled:
                if prox_zone == "near":
                    blinker.set_squint(0.55, 0.5)
                if prox_zone == "close" and cfg.get("proximity_brightness_boost", True):
                    target_br = min(100, br + 15)
                    if matrix.brightness != target_br:
                        matrix.brightness = target_br

                # zone-transition triggers (one-shot envelopes)
                if prox_zone != prev_prox_zone and prox_zone != "unknown":
                    if prev_prox_zone in ("mid", "far") and prox_zone == "close":
                        # something entered personal space - small startle
                        env_flash.trigger(0.5)
                        blinker.trigger_startled()
                        sac.react_to_sound(cfg.get("saccade_amp", 2.5) * 0.7)
                    elif prev_prox_zone == "close" and prox_zone == "near":
                        # too close - bigger startle + recoil kick
                        env_flash.trigger(0.8)
                        env_shake.trigger(0.6)
                        prox_recoil_vy += cfg.get("proximity_recoil_amp", 60.0)
                    # (no "relax" trigger on retreat - Blinker recovers naturally)
                    prev_prox_zone = prox_zone

                # eye widening on close approach
                eye_target = 1.0
                if prox_zone == "close":
                    eye_target = 1.0 + cfg.get("proximity_eye_widen", 0.18)
                elif prox_zone == "near":
                    eye_target = 0.92
                prox_eye_scale += (eye_target - prox_eye_scale) * 6.0 * dt

                # physical recoil on near approach (spring-damped)
                recoil_stiff = cfg.get("proximity_recoil_stiffness", 70.0)
                recoil_damp  = cfg.get("proximity_recoil_damping",     6.0)
                prox_recoil_vy += (0.0 - prox_recoil_dy) * recoil_stiff * dt - prox_recoil_vy * recoil_damp * dt
                prox_recoil_dy += prox_recoil_vy * dt
            else:
                prev_prox_zone = "unknown"
                prox_recoil_dy = 0.0
                prox_recoil_vy = 0.0
                prox_eye_scale = 1.0
        except Exception as e:
            log_err(f"proximity reactions: {e}")

        # proximity emotion override
        # Override the user-selected emotion based on distance zone.
        # Mapping (configurable via settings):
        #   near  (<100 mm) -> "angry"   (too close - back off)
        #   close (<400 mm) -> "happy"   (personal space - acknowledge)
        #   mid/far/unknown -> user's chosen emotion
        user_emotion = emotion          # what was chosen, before the sensors have their say
        if proximity_enabled and mode == "emotion":
            prox_emo_near  = cfg.get("proximity_emotion_near",  "angry")
            prox_emo_close = cfg.get("proximity_emotion_close", "happy")
            # "" (or an emotion that no longer exists) = keep the chosen face in that zone
            if prox_zone == "near" and prox_emo_near in emotions.EMOTIONS:
                emotion = prox_emo_near
            elif prox_zone in ("near", "close") and prox_emo_close in emotions.EMOTIONS:
                emotion = prox_emo_close
            # mid/far/unknown -> keep user's emotion (already in `emotion`)

        with lock:
            state["behavior_state"] = "ERROR" if error_mode else bstate

        frame = np.zeros((config.H, config.W, 3), dtype=np.uint8)

        # EMOTION (and PARTY)
        if mode == "emotion":
            # ---- vignette overrides ----
            fp = animations.FaceParams()
            if behavior.vignette is not None:
                behavior.vignette.apply(fp)
            if bstate == "SLEEP":
                fp.blink_floor = max(fp.blink_floor, 0.85)

            # ---- blink ----
            if (cfg.get("startled_enabled", True) and feat.onset > 0.45
                    and bstate not in ("SLEEP",)):
                blinker.trigger_startled()
            # squint under sustained loud sound
            if feat.rms > 0.5:
                if loud_since is None:
                    loud_since = now
                elif now - loud_since > 0.5:
                    blinker.set_squint(0.5, 0.6)
            else:
                loud_since = None
            blink = blinker.update(
                enabled=bool(cfg.get("blink_enabled", True)),
                min_int=cfg.get("blink_min_interval", 3.0),
                max_int=cfg.get("blink_max_interval", 7.0),
                duration=cfg.get("blink_duration", 0.22),
                mode="sleepy" if bstate == "SLEEP" else "normal",
                double_prob=cfg.get("blink_double_prob", 0.15),
            )
            blink = max(blink, fp.blink_floor)

            # ---- saccades ----
            if cfg.get("saccade_sound_react", True) and feat.onset > 0.30:
                sac.react_to_sound(cfg.get("saccade_amp", 2.5))
            eye_dx, eye_dy = sac.update(
                enabled=bool(cfg.get("saccade_enabled", True)) and bstate != "SLEEP",
                amp=cfg.get("saccade_amp", 2.5),
                min_fix=cfg.get("saccade_min_fix", 0.6),
                max_fix=cfg.get("saccade_max_fix", 2.8),
            )

            # ---- IMU vestibulo-ocular: eyes drift opposite to head ROTATION ----
            # Driven by gyroscope rate (deg/s), not by tilt. Real VOR behavior:
            # eyes counter-rotate to keep gaze world-stable; when rotation stops,
            # eyes spring back to center.
            if state.get("esp_acc_ok") and cfg.get("imu_eye_enabled", True):
                gyro = state.get("gyro_dps", [0, 0, 0])
                gx_dps, gy_dps, gz_dps = gyro[0], gyro[1], gyro[2]
                amp = cfg.get("imu_eye_amp", 0.08)            # px per (deg/s)
                # yaw rate (gz) -> horizontal eye shift; pitch rate (gx) -> vertical
                target_x = -gz_dps * amp
                target_y = -gx_dps * amp
                if target_x >  5.0: target_x =  5.0
                if target_x < -5.0: target_x = -5.0
                if target_y >  4.0: target_y =  4.0
                if target_y < -4.0: target_y = -4.0
                stiff = cfg.get("imu_eye_stiffness", 30.0)
                damp_ = cfg.get("imu_eye_damping",    6.0)
                eye_off_vx += (target_x - eye_off_x) * stiff * dt - eye_off_vx * damp_ * dt
                eye_off_vy += (target_y - eye_off_y) * stiff * dt - eye_off_vy * damp_ * dt
                eye_off_x  += eye_off_vx * dt
                eye_off_y  += eye_off_vy * dt
                eye_dx += eye_off_x
                eye_dy += eye_off_y

            # ---- gestures and emotion picked from the sound ----
            reaction_emotion = None
            burst = False
            try:
                with lock:
                    acc_ok = bool(state.get("esp_acc_ok"))
                    gyro_now = list(state.get("gyro_dps", [0, 0, 0])) if acc_ok else None
                    impact_now = acc_ok and bool(state.get("motion_impact"))
                    if impact_now and not cfg.get("imu_ouch_enabled", True):
                        state["motion_impact"] = False      # nobody else will take the flag
                    wink_request = state.pop("wink_request", None)
                if cfg.get("gestures_enabled", True):
                    reaction_emotion, gesture = gestures.update(
                        now, gyro=gyro_now, near=(proximity_enabled and prox_zone == "near"),
                        impact=impact_now)
                    if gesture == "boop":
                        behavior.vignette = animations.Sneeze()
                    elif gesture == "pet":
                        burst = True
                    elif gesture == "knock":
                        env_shake.trigger(0.8)
                    if gesture:
                        with lock:
                            state["last_gesture"] = gesture
                if reaction_emotion is None and cfg.get("auto_emotion", True) and bstate != "SLEEP":
                    reaction_emotion = auto_emotion.update(
                        feat, now, user_emotion, raw_rms=raw_rms,
                        sleepy_after=cfg.get("auto_sleepy_after_s", 45.0))
                if wink_request is not None:
                    asym.trigger(now, "wink", 1 if wink_request in (1, "1", "right") else 0)
            except Exception as e:
                log_err(f"reactions: {e}")
            if reaction_emotion is not None and reaction_emotion not in emotions.MASKS:
                reaction_emotion = None

            # ---- IMU event overrides: impact (ouch), freefall (scream), flat (sleep) ----
            forced_emotion = None
            forced_mouth   = None
            try:
                if state.get("esp_acc_ok"):
                    # 1) OUCH on impact: latched flag, brief override + envelopes
                    if state.get("motion_impact") and cfg.get("imu_ouch_enabled", True):
                        with lock:
                            state["motion_impact"] = False
                        impact_timer = cfg.get("imu_ouch_duration_s", 0.8)
                        env_flash.trigger(1.0)
                        env_shake.trigger(1.0)
                        env_glitch.trigger(1.0)
                        env_rgbsplit.trigger(1.0)
                    if impact_timer > 0.0:
                        impact_timer = max(0.0, impact_timer - dt)
                        forced_emotion = "surprised"
                        forced_mouth   = 0.85
                    # 2) Freefall: hold surprised + wide-open mouth while it lasts
                    if state.get("motion_freefall") and cfg.get("imu_freefall_enabled", True):
                        forced_emotion = "surprised"
                        forced_mouth   = 1.0
                        env_glitch.trigger(0.6)
            except Exception as e:
                log_err(f"imu_override: {e}")

            # ---- emotion transition (+ glitch cut for contrast pairs) ----
            effective_emotion = (forced_emotion if forced_emotion is not None
                                 else reaction_emotion if reaction_emotion is not None else emotion)
            if forced_emotion is not None:
                # Snap immediately, no blend animation - startle reaction is instant
                _emo_from = forced_emotion
                _emo_to   = forced_emotion
                _emo_blend = 1.0
            elif effective_emotion != _emo_to:
                _emo_from = _emo_to if _emo_blend >= 1.0 else _emo_from
                _emo_to = effective_emotion
                _emo_blend = 0.0 if effective_emotion != _emo_from else 1.0
                if frozenset((_emo_from, _emo_to)) in CONTRAST_PAIRS:
                    env_glitch.trigger(1.0)
                    env_rgbsplit.trigger(0.8)
                if _emo_blend < 1.0:
                    env_zoom.trigger(cfg.get("emotion_pop", 0.6))    # the new face pops in
                    env_rgbsplit.trigger(0.45)
                    arrival_pending = True
            trans_speed = cfg.get("emotion_transition_speed", 0.08)
            if _emo_blend < 1.0:       # each emotion arrives at its own pace
                _emo_blend = min(1.0, _emo_blend + trans_speed
                                 * getattr(emotions, "transition_speed", lambda e: 1.0)(_emo_to))
            arrived = arrival_pending and _emo_blend >= 0.7      # the new eye has just opened
            if arrived:
                arrival_pending = False

            # ---- dizzy trigger: sustained shake -> spinning spirals + open mouth ----
            dizzy_level = 0.0
            if cfg.get("imu_dizzy_enabled", True) and state.get("esp_acc_ok"):
                trigger_s = cfg.get("imu_dizzy_trigger_s",  2.0)
                duration_s = cfg.get("imu_dizzy_duration_s", 3.0)
                # Pi-side shake detection: |a|-1 g smoothed, beats firmware's binary flag
                acc = state.get("accel_mg", [0, 0, 1000])
                mag_g = math.sqrt(acc[0]*acc[0] + acc[1]*acc[1] + acc[2]*acc[2]) / 1000.0
                shake_ema = shake_ema * 0.88 + abs(mag_g - 1.0) * 0.12
                is_shaking = shake_ema > cfg.get("imu_dizzy_shake_threshold", 0.12)
                if is_shaking:
                    shake_accum_t = min(shake_accum_t + dt, trigger_s + 0.5)
                else:
                    shake_accum_t = max(0.0, shake_accum_t - dt * 0.5)
                if shake_accum_t >= trigger_s:
                    dizzy_remaining = duration_s
                elif dizzy_remaining > 0:
                    dizzy_remaining = max(0.0, dizzy_remaining - dt)
                if dizzy_remaining > 0:
                    elapsed = duration_s - dizzy_remaining
                    fade_in  = min(1.0, elapsed * 3.3)         # ~0.3 s ramp-up
                    fade_out = min(1.0, dizzy_remaining / 0.6) # ~0.6 s ramp-down
                    dizzy_level = min(fade_in, fade_out)
                dizzy_phase += dt * cfg.get("imu_dizzy_spin_rate", 8.0)

            # ---- lip-sync (+ bass mouth kick + dizzy mouth) ----
            lo = int(cfg.get("voice_band_lo", 2))
            hi = int(cfg.get("voice_band_hi", 10))
            lo = max(0, min(lo, bands - 1))
            hi = max(lo + 1, min(hi, bands))
            avg = float(bars_now[lo:hi].mean())
            voice_target = 0.0
            if avg >= cfg.get("voice_threshold", 0.05):
                voice_target = min(1.0, avg * cfg.get("voice_gain", 3.0))
            kick = env_mouth.update(dt) * cfg.get("beat_mouth_kick", 0.3)
            target_open = max(voice_target, kick, dizzy_level * 0.9)
            if forced_mouth is not None:
                target_open = max(target_open, forced_mouth)
            if bstate == "SLEEP":
                target_open = 0.0
            rate = (cfg.get("lipsync_attack_open", 0.55)
                    if target_open > mouth_open_level
                    else cfg.get("lipsync_attack_close", 0.20))
            mouth_open_level += (target_open - mouth_open_level) * rate
            if fp.mouth_override is not None:
                mouth_open_level = fp.mouth_override

            # ---- character of the emotion: idle motion, speech and beat accents ----
            live = liveliness.motion(
                _emo_from, _emo_to, _emo_blend, now - t0,
                mouth=mouth_open_level, beat=env_live.update(dt),
                amount=0.0 if bstate == "SLEEP" else cfg.get("liveliness", 1.0))
            # with a pupil the gaze belongs to the pupil; the eye itself barely moves
            pupil_dx = pupil_dy = 0.0
            if getattr(emotions, "has_pupil", lambda e: False)(_emo_to) and cfg.get("pupil_gaze", True):
                gain = cfg.get("pupil_gaze_gain", 1.3)
                pupil_dx = max(-2.5, min(2.5, eye_dx * gain))       # stays inside the eye
                pupil_dy = max(-1.5, min(1.5, eye_dy * gain))
                eye_dx *= 0.2
                eye_dy *= 0.2
            eye_dx += live.eye_dx
            eye_dy += live.eye_dy
            blink = 1.0 - (1.0 - blink) * (1.0 - live.eye_squint)
            # where the eye and the mouth of this face are (None: polygon faces)
            lay = emotions.get_layout(_emo_to)
            lay_from = emotions.get_layout(_emo_from) or lay
            lay_t = min(1.0, _emo_blend)
            grow = ({"eye_grow": live.eye_grow, "mouth_grow": live.mouth_grow}
                    if lay is not None else {})
            if pupil_dx or pupil_dy:
                grow["pupil_dx"], grow["pupil_dy"] = pupil_dx, pupil_dy
            mouth_round, mouth_wide = visemes.update(bars_now)
            if lay is not None and cfg.get("visemes_enabled", True):
                grow["mouth_round"], grow["mouth_wide"] = mouth_round, mouth_wide
            # winks and doubtful looks: (closure, lift) for the eye of each panel
            eye_a, eye_b = asym.update(
                now, _emo_to,
                enabled=bool(cfg.get("asymmetry_enabled", True)) and bstate not in ("SLEEP",)
                        and _emo_blend >= 1.0)

            # ---- face alpha ----
            alpha = emotions.get_face_alpha_animated(
                _emo_from, _emo_to, _emo_blend,
                mouth_open_level,
                blink_level=blink,
                eye_dx=eye_dx, eye_dy=eye_dy,
                nose_dy=fp.nose_dy + live.nose_dy,
                **grow,
            )

            # ---- beats -> envelopes ----
            if cfg.get("beat_enabled", True) and feat.beat > 0.0:
                env_zoom.trigger(feat.beat)
                env_flash.trigger(cfg.get("beat_flash_strength", 0.35) * feat.beat)
                env_mouth.trigger(feat.beat)
                env_live.trigger(feat.beat)
                if feat.beat > 0.7:
                    env_shake.trigger(feat.beat)
                if cfg.get("fx_rgbsplit_beat", True):
                    env_rgbsplit.trigger(feat.beat * 0.6)
                if party:
                    env_hue.trigger(1.0)

            # ---- colorize over background ----
            hue_kick = env_hue.update(dt) * (30.0 / 360.0)
            if effect == "solid":
                face_rgb = np.broadcast_to(
                    np.array(color, dtype=np.float32), (config.H, config.W, 3))
            elif effect == "gradient":
                face_rgb = _make_gradient_map(grad_colors, grad_angle)
            elif effect == "rainbow":
                speed = cfg.get("rainbow_speed", 40.0)
                offset = (now - t0) * speed
                face_rgb = _make_rainbow_map(diag, offset)
            elif effect == "audio":
                face_rgb = np.broadcast_to(
                    _audio_color(feat.centroid, feat.rms_slow, hue_kick),
                    (config.H, config.W, 3))
            else:
                face_rgb = np.broadcast_to(
                    np.array(color, dtype=np.float32), (config.H, config.W, 3))

            bg = (_party_background(bars_now, now) if (party or bstate == "PARTY")
                  else np.zeros((config.H, config.W, 3), dtype=np.float32))
            a3 = alpha[:, :, np.newaxis]
            out = bg * (1.0 - a3) + face_rgb * a3

            # ---- aura outline ----
            if cfg.get("aura_enabled", True):
                if effect == "solid":
                    aura_col = color
                elif effect == "audio":
                    aura_col = _audio_color(feat.centroid, feat.rms_slow, hue_kick)
                else:
                    aura_col = (180, 220, 255)
                aura_boost = 1.6 if (party or bstate == "PARTY") else 1.0
                intensity = cfg.get("aura_intensity", 0.8) * (0.25 + feat.rms * 1.5) * aura_boost
                out += postfx.aura_outline(alpha, aura_col, intensity)

            frame = np.clip(out, 0, 255).astype(np.uint8)

            # ---- catchlight: small white highlight on the eye for liveliness ----
            if cfg.get("catchlight_enabled", True) and blink < 0.55 and dizzy_level < 0.2 and bstate != "SLEEP":
                try:
                    base_x, base_y = 47.0, 11.0
                    if lay is not None:
                        base_x = lay_from.catchlight[0] * (1.0 - lay_t) + lay.catchlight[0] * lay_t
                        base_y = lay_from.catchlight[1] * (1.0 - lay_t) + lay.catchlight[1] * lay_t
                    trail = cfg.get("catchlight_trail", 0.35)
                    cx = int(round(base_x + eye_dx * (1.0 - trail)))
                    cy = int(round(base_y + eye_dy * (1.0 - trail)))
                    if (0 <= cx < config.W and 0 <= cy < config.H
                            and (lay is None or alpha[cy, cx] > 0.5)):   # only on the eye itself
                        frame[cy, cx] = (255, 255, 255)
                        if cfg.get("catchlight_double", True) and cx + 1 < config.W:
                            frame[cy, cx + 1] = (220, 220, 220)
                except Exception as e:
                    log_err(f"catchlight: {e}")

            # ---- eye sparkles ----
            if cfg.get("sparkle_enabled", True):
                if feat.onset > 0.30 and len(sparkles) < 3:
                    coords = _eye_coords(_emo_to)
                    if coords:
                        x, y = random.choice(coords)
                        sparkles.append([x, y, 3])
                if fp.sparkle_burst or arrived or burst:
                    coords = _eye_coords(_emo_to)
                    for _ in range(6):
                        if coords:
                            x, y = random.choice(coords)
                            sparkles.append([x + random.randint(-3, 3),
                                             y + random.randint(-3, 3), 4])
                alive = []
                for s in sparkles:
                    x, y, life = s
                    if 0 <= x < config.W and 0 <= y < config.H and life > 0:
                        frame[y, x] = (255, 255, 255)
                        s[2] -= 1
                        if s[2] > 0:
                            alive.append(s)
                sparkles = alive

            # ---- beat flash brightness ----
            flash = env_flash.update(dt, decay=cfg.get("beat_flash_decay", 6.0))
            if flash > 0.01:
                frame = add_flash(frame, flash, flash_mask)

            # ---- EQ bars inside open mouth ----
            mouth_eq_threshold = cfg.get("mouth_eq_threshold", 0.5)
            if cfg.get("mouth_eq_enabled", True) and mouth_open_level > mouth_eq_threshold:
                try:
                    mouth_mask = emotions.MASKS.get(
                        _emo_to, emotions.MASKS["angry"]).mouth_open
                    y0, y1 = 18, 30
                    x0, x1 = 0, 40
                    if lay is not None:
                        x0, y0, x1, y1 = lay.mouth_box
                    n_bars = 8
                    bar_w  = (x1 - x0) // n_bars
                    src = bars_now[:n_bars]
                    intensity = min(1.0, (mouth_open_level - mouth_eq_threshold) * 2.0)
                    bar_color = np.array([255, 255, 255], dtype=np.float32)
                    for bi in range(n_bars):
                        v = float(src[bi])
                        h = int(round(v * (y1 - y0) * 0.9))
                        if h <= 0:
                            continue
                        bx0 = x0 + bi * bar_w
                        bx1 = min(x0 + (bi + 1) * bar_w - 1, x1 - 1)
                        by1 = y1 - 1
                        by0 = max(y0, by1 - h)
                        region = frame[by0:by1 + 1, bx0:bx1 + 1].astype(np.float32)
                        clip = mouth_mask[by0:by1 + 1, bx0:bx1 + 1].astype(np.float32) / 255.0
                        a = clip[:, :, None] * intensity
                        region = region * (1.0 - a) + bar_color[None, None, :] * a
                        frame[by0:by1 + 1, bx0:bx1 + 1] = region.astype(np.uint8)
                except Exception as e:
                    log_err(f"mouth_eq: {e}")

            # ---- dizzy: erase eye then draw spinning spiral (drawn before affine) ----
            if dizzy_level > 0.02:
              try:
                # eye is on the right half of the panel - mirror chain duplicates it
                cx = (lay.eye_centre[0] if lay is not None else 50.0) + eye_dx
                cy = (lay.eye_centre[1] if lay is not None else 13.0) + eye_dy
                max_r = float(cfg.get("imu_dizzy_radius", 10.0))
                turns = float(cfg.get("imu_dizzy_turns", 3.0))
                # 1) erase the eye region down toward bg (face color -> 0 at dizzy_level=1)
                erase_r = max_r + 1.0
                y0 = max(0, int(cy - erase_r));  y1 = min(config.H, int(cy + erase_r + 1))
                x0 = max(0, int(cx - erase_r));  x1 = min(config.W, int(cx + erase_r + 1))
                if y1 > y0 and x1 > x0:
                    yy, xx = np.ogrid[y0:y1, x0:x1]
                    inside = ((xx - cx) ** 2 + (yy - cy) ** 2) <= (erase_r * erase_r)
                    mask = inside.astype(np.float32) * dizzy_level
                    region = frame[y0:y1, x0:x1].astype(np.float32)
                    region *= (1.0 - mask[:, :, None])
                    frame[y0:y1, x0:x1] = region.astype(np.uint8)
                # 2) draw the spiral on top - denser samples, slightly thickened
                spiral_color = (255, 255, 255)
                n = int(max_r * 18)
                a_pixel = dizzy_level
                for i in range(n):
                    frac = i / float(n)
                    t = frac * turns * 2.0 * math.pi
                    r = frac * max_r
                    x = int(round(cx + r * math.cos(t + dizzy_phase)))
                    y = int(round(cy + r * math.sin(t + dizzy_phase)))
                    if 0 <= x < config.W and 0 <= y < config.H:
                        cur = frame[y, x]
                        frame[y, x, 0] = int(cur[0] * (1.0 - a_pixel) + spiral_color[0] * a_pixel)
                        frame[y, x, 1] = int(cur[1] * (1.0 - a_pixel) + spiral_color[1] * a_pixel)
                        frame[y, x, 2] = int(cur[2] * (1.0 - a_pixel) + spiral_color[2] * a_pixel)
              except Exception as e:
                log_err(f"dizzy: {e}")

            # ---- single affine: breathing + tilt + beat zoom/shake ----
            breath_speed = cfg.get("breath_speed", 1.0) * (0.6 if bstate == "SLEEP" else 1.0)
            dx, dy = _breathing_offset(
                now - t0, cfg.get("breath_amp_x", 1.0),
                cfg.get("breath_amp_y", 1.0), breath_speed)
            dx += fp.dx_mod
            dy += fp.dy_mod
            shake = env_shake.update(dt) * cfg.get("beat_shake_amp", 1.5)
            if shake > 0.02:
                dx += random.uniform(-shake, shake)
                dy += random.uniform(-shake, shake) * 0.5
            zoom = fp.scale_mod
            zoom *= 1.0 + env_zoom.update(dt) * cfg.get("beat_zoom_amp", 0.06)
            zoom *= 1.0 + _breathing_scale(now - t0, breath_speed) * 0.02 * cfg.get("breath_scale_amp", 1.0)
            angle = 0.0
            if cfg.get("tilt_enabled", True):
                # curious tilt when speech appears after long silence
                if was_long_silence and feat.speech > 0.15:
                    tilt_sign = random.choice([-1.0, 1.0])
                    env_tilt.trigger(1.0)
                angle = (tilt_lfo.value(now - t0) * cfg.get("tilt_amp", 1.5)
                         + env_tilt.update(dt) * 3.0 * tilt_sign)

            # ---- IMU step bob: detect spike on accel MAGNITUDE -> spring-kick Y ----
            # Mount-agnostic: doesn't matter which axis is "down", we look at the
            # overall acceleration vector length deviating from ~1 g.
            if state.get("esp_acc_ok") and cfg.get("imu_bob_enabled", True):
                ax_, ay_, az_ = state.get("accel_mg", [0, 0, 1000])
                mag_g = math.sqrt(ax_*ax_ + ay_*ay_ + az_*az_) / 1000.0
                az_lp = az_lp * 0.90 + mag_g * 0.10          # baseline ~ 1.0 g
                dev = mag_g - az_lp
                threshold  = cfg.get("imu_bob_threshold",  0.12)
                refractory = cfg.get("imu_bob_refractory", 0.22)
                if dev > threshold and (now - last_step_t) > refractory:
                    bob_vy += min(dev, 1.5) * cfg.get("imu_bob_amp", 4.0)
                    last_step_t = now
                bob_stiff = cfg.get("imu_bob_stiffness", 110.0)
                bob_damp  = cfg.get("imu_bob_damping",     3.5)
                bob_vy += (0.0 - bob_y) * bob_stiff * dt - bob_vy * bob_damp * dt
                bob_y  += bob_vy * dt
                dy += bob_y

            # proximity-driven recoil offset (face leans back from too-close)
            dy += prox_recoil_dy

            was_long_silence = feat.silence_time > 5.0

            sway_dx = dx
            coeffs = _build_affine(zoom, angle, dx, dy, config.W / 2.0, config.H / 2.0)
            # np.asarray on PIL.Image returns a read-only view, which breaks
            # any draw_text / overlay that writes into the frame afterwards.
            # np.array(..., copy=True) makes a fresh writable buffer.
            frame = np.array(
                Image.fromarray(frame).transform(
                    (config.W, config.H), Image.AFFINE, coeffs, Image.BILINEAR),
                copy=True)

            # ---- SLEEP overlay: floating Zzz ----
            if bstate == "SLEEP":
                if now - last_zzz_t > 2.2:
                    last_zzz_t = now
                    zx0, zy0 = lay.eye_centre if lay is not None else (48.0, 13.0)
                    zzz.append({"x": zx0, "y": zy0, "t0": now})
                if not frame.flags.writeable:
                    frame = frame.copy()
                alive_z = []
                for z in zzz:
                    age = now - z["t0"]
                    if age < 2.5:
                        zx = int(z["x"] + age * 3.0)
                        zy = int(z["y"] - age * 4.0)
                        bright = int(200 * (1.0 - age / 2.5))
                        boot_animation.draw_text(frame, zx, zy, "Z",
                                                 (0, max(40, bright), bright // 3))
                        alive_z.append(z)
                zzz = alive_z
            else:
                zzz.clear()

            # ---- ERROR overlay ----
            if error_mode:
                frame = (frame.astype(np.float32) * np.array([1.0, 0.25, 0.25])).astype(np.uint8)
                if (now % 0.7) < 0.10:
                    frame = postfx.glitch_lines(frame, 1.0)
                if not frame.flags.writeable:
                    frame = frame.copy()
                boot_animation.draw_text(frame, 1, 1, "ERR", (255, 70, 70))

            # ---- post-fx chain ----
            g = env_glitch.update(dt)
            if g > 0.03:
                frame = postfx.glitch_lines(frame, g)
            rs = env_rgbsplit.update(dt)
            if rs > 0.05:
                frame = postfx.rgb_split(frame, rs * 2.0)
            sl = cfg.get("fx_scanline", 0.4) * (1.3 if (party or bstate == "PARTY") else 1.0)
            frame = postfx.scanline(frame, min(1.0, sl))
            frame = postfx.film_noise(frame, cfg.get("fx_noise", 0.15))
            bl = cfg.get("fx_bloom", 0.0)
            if bl > 0.01 and frame_no % 2 == 0:
                frame = postfx.bloom(frame, bl)

            # ---- the two eyes: same gaze on both panels, winks on one ----
            dual = mir and config.CHAIN_LENGTH >= 2
            parallel = (dual and cfg.get("eyes_parallel", True)
                        and (abs(eye_dx) > 0.05 or abs(sway_dx) > 0.05 or abs(pupil_dx) > 0.05))
            odd_a = eye_a[0] > 0.01 or abs(eye_a[1]) > 0.02
            odd_b = dual and (eye_b[0] > 0.01 or abs(eye_b[1]) > 0.02)
            if lay is not None and dizzy_level < 0.02 and (parallel or odd_a or odd_b):
                try:
                    halo = emotions.get_eye_halo(
                        _emo_from, _emo_to, _emo_blend, blink_level=blink,
                        eye_dx=eye_dx, eye_dy=eye_dy, eye_grow=live.eye_grow)
                    pupil_move = None
                    if parallel and abs(pupil_dx) > 0.05:
                        # the mirrored panel needs the pupil on the other side of the eye
                        old, new = emotions.get_pupil_holes(
                            _emo_from, _emo_to, _emo_blend, blink_level=blink,
                            eye_dx=eye_dx, eye_dy=eye_dy, eye_grow=live.eye_grow,
                            gazes=((pupil_dx, pupil_dy), (-pupil_dx, pupil_dy)))
                        three = _halo_on_panel(np.stack([halo, old, new], axis=2), coeffs)
                        eye_mask = three[:, :, 0:1]
                        pupil_move = (three[:, :, 1:2], three[:, :, 2:3])
                    else:
                        eye_mask = _halo_on_panel(halo, coeffs)
                    plain = frame
                    if parallel or odd_b:
                        frame_b = _eye_variant(
                            plain, eye_mask,
                            shift=-2.0 * eye_dx if parallel else 0.0,
                            closure=eye_b[0], lift=eye_b[1],
                            sway=-2.0 * sway_dx if parallel else 0.0, pupil=pupil_move)
                    if odd_a:
                        frame = _eye_variant(plain, eye_mask, closure=eye_a[0], lift=eye_a[1])
                except Exception as e:
                    log_err(f"eyes: {e}")

            with lock:
                state["mouth_open_level"] = round(mouth_open_level, 2)
                state["emotion_from"] = _emo_from
                state["emotion_blend"] = round(_emo_blend, 3)
                state["emotion_shown"] = _emo_to
                state["auto_gain_now"] = round(agc.gain, 1)
                state["is_blinking"] = blink > 0.01
                state["beat_flash"] = round(flash, 2)

        # IMAGE / GIF
        elif mode == "image":
            with lock:
                gif_frames = list(uploaded_gif_frames)
                still = None if gif_frames else uploaded_image.copy()
                state["gif_frame_count"] = len(gif_frames)
            if gif_frames:
                # A new, shorter animation may have been uploaded since the
                # last frame: start it from the beginning.
                if gif_idx >= len(gif_frames):
                    gif_idx, gif_t = 0, 0.0
                gif_t += dt
                cur_frame, cur_delay = gif_frames[gif_idx]
                if gif_t >= cur_delay:
                    gif_t = 0.0
                    gif_idx = (gif_idx + 1) % len(gif_frames)
                frame = cur_frame.copy()
            else:
                frame = still

        # TEXT
        elif mode == "text":
            speed   = cfg.get("text_speed", 24.0)
            scale   = max(1, int(cfg.get("text_scale", 1)))
            effect  = cfg.get("text_effect", "solid")
            full_w  = config.W * config.CHAIN_LENGTH if (mir and config.CHAIN_LENGTH >= 2) else config.W
            frame = _render_text(text_content, (now - t0) * speed,
                                  tuple(text_color), full_w,
                                  scale=scale, effect=effect, t=now)

        # DINO GAME
        elif mode == "dino":
            full_w = config.W * config.CHAIN_LENGTH if (mir and config.CHAIN_LENGTH >= 2) else config.W
            game = dino_game.get(full_w, config.H)
            # Push tunable settings into the game instance every frame
            game.dino_x      = max(0, min(full_w - 8, int(cfg.get("dino_x", 24))))
            game.jump_v0     = float(cfg.get("dino_jump_v", 92.0))
            game.gravity     = float(cfg.get("dino_gravity", 320.0))
            game.base_speed  = float(cfg.get("dino_speed_base", 32.0))
            game.speed_ramp  = float(cfg.get("dino_speed_ramp", 0.0009))
            game.spawn_min_s = float(cfg.get("dino_spawn_min_s", 0.7))
            game.spawn_max_s = float(cfg.get("dino_spawn_max_s", 1.6))
            tof_enabled = bool(cfg.get("dino_tof_enabled", True))
            tof_lo = float(cfg.get("dino_tof_lo_mm", 50))
            tof_hi = float(cfg.get("dino_tof_hi_mm", 150))
            with lock:
                jump = state.get("dino_jump", False)
                if jump:
                    state["dino_jump"] = False
                dist_mm = state.get("distance_smoothed_mm") or state.get("distance_mm")
                dist_valid = state.get("distance_valid", False) and (dist_mm is not None)
            # ToF jump - edge-triggered with hysteresis
            if tof_enabled and dist_valid:
                if _dino_tof_armed and dist_mm < tof_lo:
                    jump = True
                    _dino_tof_armed = False
                elif (not _dino_tof_armed) and dist_mm > tof_hi:
                    _dino_tof_armed = True
            if jump:
                game.jump()
            game.step(dt)
            frame = game.render()
            with lock:
                state["dino_score"] = game.score
                state["dino_high"]  = game.high

        # EQ
        elif mode == "eq":
            spec.push(bars_now)
            if effect == "spectrogram":
                frame = _render_spectrogram(spec.as_image(), cfg.get("spec_gain", 1.4))
            else:
                eq_a = cfg.get("eq_attack", 0.95)
                eq_d = cfg.get("eq_decay", 0.45)
                eq_g = cfg.get("eq_peak_gravity", 2.2)
                for i in range(bands):
                    if bars_now[i] > display[i]:
                        display[i] += (bars_now[i] - display[i]) * eq_a
                    else:
                        display[i] += (bars_now[i] - display[i]) * eq_d
                    if display[i] > peaks[i]:
                        peaks[i] = display[i]
                        peak_v[i] = 0
                    else:
                        peak_v[i] += eq_g * dt
                        peaks[i] -= peak_v[i] * dt
                        if peaks[i] < 0:
                            peaks[i] = 0
                for i in range(bands):
                    bar_h = int(display[i] * config.H)
                    py = int(round((1 - peaks[i]) * (config.H - 1)))
                    x0 = i * bar_w
                    x1 = x0 + bar_w - gap
                    if bar_h > 0:
                        frame[config.H - bar_h:config.H, x0:x1] = (
                            color_grad[config.H - bar_h:config.H, np.newaxis, :])
                    if 0 <= py < config.H and peaks[i] > 0.02:
                        frame[py, x0:x1] = (255, 255, 255)
        # COLOUR MUSIC
        elif mode == "music":
            frame = colormusic.render(bars_now, feat.beat, dt, cfg.get("music_style", "auto"),
                                      config.W, config.H)

        # compose dual panel / rotate / push
        # Skip mirror-compose if the frame is ALREADY at full chained width
        # (text mode renders directly to 128x32 for continuous scrolling).
        if mir and config.CHAIN_LENGTH >= 2 and frame.shape[1] < config.W * config.CHAIN_LENGTH:
            if frame_b is not None:
                frame = np.concatenate([frame, frame_b[:, ::-1, :]], axis=1)
            else:
                frame = _compose_dual(frame)
        frame, power_load = _limit_power(frame, matrix.brightness, cfg.get("power_limit", 0.25))
        if frame_no % 15 == 0:
            with lock:
                state["power_load"] = round(power_load, 3)
        shared.preview["frame"], shared.preview["seq"] = frame, frame_no     # for the web page
        img = Image.fromarray(frame)
        if rot:
            img = img.transpose(Image.ROTATE_180)
        canvas.SetImage(img)
        canvas = matrix.SwapOnVSync(canvas)

        el = time.monotonic() - now
        if el < frame_dt:
            time.sleep(frame_dt - el)

    matrix.Clear()


def start():
    threading.Thread(target=_run, daemon=True, name="matrix-renderer").start()
