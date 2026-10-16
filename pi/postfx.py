"""Post-processing effects for the final RGB frame (numpy uint8 HxWx3).

Every effect takes the frame plus an intensity 0..1 so envelopes can
animate them (e.g. rgb_split driven by a beat envelope).
"""
import random

import numpy as np
from PIL import Image, ImageFilter


def scanline(frame, k=1.0):
    """Darken every even row - cheap CRT look."""
    if k <= 0.01:
        return frame
    out = frame.copy()
    out[::2] = (out[::2].astype(np.float32) * (1.0 - 0.18 * k)).astype(np.uint8)
    return out


def film_noise(frame, k=1.0):
    """Per-pixel brightness jitter on a small fraction of pixels."""
    if k <= 0.01:
        return frame
    h, w = frame.shape[:2]
    n = int(h * w * 0.05 * k)
    if n <= 0:
        return frame
    out = frame.copy()
    for _ in range(n):
        y = random.randrange(h)
        x = random.randrange(w)
        d = random.randint(-10, 10)
        out[y, x] = np.clip(out[y, x].astype(np.int16) + d, 0, 255).astype(np.uint8)
    return out


def rgb_split(frame, px):
    """Chromatic aberration: R shifted right, B shifted left by px pixels."""
    px = int(round(px))
    if px == 0:
        return frame
    out = frame.copy()
    out[:, :, 0] = np.roll(frame[:, :, 0], px, axis=1)
    out[:, :, 2] = np.roll(frame[:, :, 2], -px, axis=1)
    return out


def bloom(frame, k=0.5, threshold=190):
    """Pseudo-bloom: blur the bright areas and add them back."""
    if k <= 0.01:
        return frame
    f = frame.astype(np.float32)
    mask = (f.max(axis=2, keepdims=True) >= threshold).astype(np.float32)
    bright = (f * mask).astype(np.uint8)
    blurred = np.asarray(
        Image.fromarray(bright).filter(ImageFilter.GaussianBlur(1.4)),
        dtype=np.float32,
    )
    return np.clip(f + blurred * 0.5 * k, 0, 255).astype(np.uint8)


def glitch_lines(frame, k=1.0):
    """Random horizontal band shifts + occasional inverted block."""
    if k <= 0.02:
        return frame
    out = frame.copy()
    h, w = out.shape[:2]
    n_bands = max(1, int(4 * k))
    for _ in range(n_bands):
        y = random.randrange(0, h - 2)
        bh = random.randrange(1, 4)
        shift = random.randint(-int(6 * k) - 1, int(6 * k) + 1)
        if shift:
            out[y:y + bh] = np.roll(out[y:y + bh], shift, axis=1)
    if random.random() < 0.3 * k:
        bx = random.randrange(0, max(1, w - 8))
        by = random.randrange(0, max(1, h - 8))
        out[by:by + 8, bx:bx + 8] = 255 - out[by:by + 8, bx:bx + 8]
    return out


def aura_outline(alpha, color, intensity=1.0):
    """1-px glow outline around the face mask.

    alpha - float array (H, W) in 0..1; returns float RGB overlay to add.
    """
    a8 = (np.clip(alpha, 0.0, 1.0) * 255).astype(np.uint8)
    dil = np.asarray(
        Image.fromarray(a8).filter(ImageFilter.MaxFilter(3)),
        dtype=np.float32,
    )
    outline = np.clip(dil - a8.astype(np.float32), 0, 255) / 255.0
    col = np.asarray(color, dtype=np.float32)
    return outline[:, :, None] * col[None, None, :] * max(0.0, intensity)
