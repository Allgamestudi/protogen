"""Uploaded picture / GIF -> 64x32 frames: convert_upload(raw, 64, 32).

Returns [(HxWx3 uint8, delay_s), ...]. Handles transparency (on black), pixel
art scaling, EXIF rotation, odd pixel formats, 0-delay GIF frames (as 10 fps).
"""
from io import BytesIO

import numpy as np
from PIL import Image, ImageOps

MAX_FRAMES = 600            # ~1 MB of frames at 64x32; a longer animation is cut
MAX_SIDE = 4096             # refuse absurd pictures before decoding eats the RAM
MIN_DELAY_MS = 20           # anything faster is treated as "no delay given"
DEFAULT_DELAY_MS = 100

_MAGIC = (
    (b"GIF87a", "GIF"), (b"GIF89a", "GIF"), (b"\x89PNG\r\n\x1a\n", "PNG"),
    (b"\xff\xd8\xff", "JPEG"), (b"BM", "BMP"),
)


class ConvertError(ValueError):
    """The upload cannot be shown; the message is meant for the user."""


def sniff_format(raw):
    for magic, fmt in _MAGIC:
        if raw.startswith(magic):
            return fmt
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "WEBP"
    if raw[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftypavif"):
        return "HEIC"
    return None


def _to_rgb_on_black(img):
    """Any Pillow mode -> RGB, transparent areas black."""
    if img.mode in ("I;16", "I;16L", "I;16B", "I", "F"):
        # high bit depth: stretch to 8 bits instead of clipping to white
        arr = np.asarray(img, dtype=np.float32)
        lo, hi = float(arr.min()), float(arr.max())
        scale = 255.0 / (hi - lo) if hi > lo else 0.0
        img = Image.fromarray(((arr - lo) * scale).astype(np.uint8), "L")
    has_alpha = img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info
    if not has_alpha:
        return img.convert("RGB")
    rgba = img.convert("RGBA")
    black = Image.new("RGBA", rgba.size, (0, 0, 0, 255))
    return Image.alpha_composite(black, rgba).convert("RGB")


def fit_to_panel(img, w, h):
    """Scale to fit w x h, keep the proportions, centre on black."""
    rgb = _to_rgb_on_black(img)
    if rgb.size != (w, h):
        scale = min(w / rgb.width, h / rgb.height)
        new_w = max(1, min(w, round(rgb.width * scale)))
        new_h = max(1, min(h, round(rgb.height * scale)))
        # enlarging: keep pixel art crisp; reducing: average properly
        method = Image.NEAREST if scale >= 1.0 else Image.LANCZOS
        rgb = rgb.resize((new_w, new_h), method)
    if rgb.size == (w, h):
        return rgb
    canvas = Image.new("RGB", (w, h), (0, 0, 0))
    canvas.paste(rgb, ((w - rgb.width) // 2, (h - rgb.height) // 2))
    return canvas


def _delay_s(info):
    ms = info.get("duration", 0) or 0
    if ms < MIN_DELAY_MS:
        ms = DEFAULT_DELAY_MS
    return ms / 1000.0


def convert_upload(raw, w, h):
    """Decode `raw` (bytes of an image file) into panel frames.

    Returns (frames, info). Raises ConvertError with a readable message.
    """
    if not raw:
        raise ConvertError("empty file")
    fmt = sniff_format(raw)
    if fmt == "HEIC":
        raise ConvertError("HEIC/AVIF photos are not supported: save as JPEG or PNG")
    try:
        src = Image.open(BytesIO(raw), formats=[fmt] if fmt else None)
    except Exception as e:
        head = " ".join(f"{b:02x}" for b in raw[:8])
        raise ConvertError(f"not a picture this device can read (starts with {head}): {e}")
    if max(src.size) > MAX_SIDE * 4:
        raise ConvertError(f"picture is too large: {src.width}x{src.height}")

    n_frames = getattr(src, "n_frames", 1)
    animated = bool(getattr(src, "is_animated", False)) and n_frames > 1
    frames = []
    try:
        if animated:
            for i in range(min(n_frames, MAX_FRAMES)):
                src.seek(i)                 # Pillow composes partial GIF frames itself
                frame = fit_to_panel(src, w, h)
                frames.append((np.asarray(frame, dtype=np.uint8).copy(), _delay_s(src.info)))
        else:
            src.load()
            upright = ImageOps.exif_transpose(src) or src
            frames.append((np.asarray(fit_to_panel(upright, w, h), dtype=np.uint8).copy(), 0.0))
    except ConvertError:
        raise
    except Exception as e:
        raise ConvertError(f"could not decode {fmt or 'the file'}: {e}")
    info = {
        "format": fmt or (src.format or "?"),
        "animated": animated,
        "frames": len(frames),
        "source_frames": n_frames,
        "truncated": animated and n_frames > MAX_FRAMES,
        "source_size": [src.width, src.height],
    }
    return frames, info


def convert_mask(raw, w, h):
    """An uploaded picture as a face part: H x W float32, 0 = off, 1 = lit.

    Meant for 64 x 32 pictures drawn light on dark (like the artwork), with or
    without transparency. Other sizes are fitted, colour is ignored, and a
    picture drawn dark on light is inverted.
    """
    frames, _ = convert_upload(raw, w, h)
    mask = frames[0][0].max(axis=2).astype(np.float32) / 255.0
    if float(mask.mean()) > 0.5:
        mask = 1.0 - mask
    return mask

