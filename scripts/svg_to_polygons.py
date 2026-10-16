"""Extract polygon vertex data from the per-component SVG artwork.

The artist ships ``{ru_name}_{close|open}_{eye|mouth}.svg`` plus a shared
``nose.svg`` in ``Pictures/proto/svg/face``. Each SVG is a 200x100 mm
canvas - same 2:1 aspect ratio as one 64x32 LED panel.

This tool walks every emotion, samples each component's visible path
into a polyline, and uniformly scales 200x100 mm -> 64x32 px (factor 0.32
on BOTH axes - no horizontal flip, no aspect distortion). Vertices land
at the same relative position they have in the SVG, so the rendered
face matches the original artwork 1:1.

Output is printed to stdout. Pipe it into a file and paste the polygon
section into ``pi/emotions.py``.
"""
from __future__ import annotations

import sys
from pathlib import Path

from svgpathtools import CubicBezier, Line, QuadraticBezier, svg2paths


SRC_DIR = Path(r"C:\Users\User\Pictures\proto\svg\face")

# emotion_id -> filename prefix in SRC_DIR.
EMOTIONS_MAP = {
    "angry":     "angry",
    "bad":       "bad",
    "happy":     "happy",
    "sad":       "sad",
    "scared":    "испуг",
    "surprised": "удивление",
}

EMOJI_LABEL = {
    "angry":     ("\\U0001F620", "Angry"),
    "bad":       ("\\U0001F608", "Bad"),
    "happy":     ("\\U0001F60A", "Happy"),
    "sad":       ("\\U0001F622", "Sad"),
    "scared":    ("\\U0001F631", "Испуг"),
    "surprised": ("\\U0001F632", "Удивление"),
}

# Uniform scale: SVG 200x100mm -> buffer 64x32px -> factor 0.32 on both axes.
SCALE = 0.32

# Samples per cubic bezier - higher = smoother contour.
SAMPLES_PER_CURVE = 8


def _pick_visible_path(svg_file: Path):
    """Return the longest visible path in the SVG, or None."""
    paths, attrs = svg2paths(str(svg_file))
    visible = []
    for p, a in zip(paths, attrs):
        style = a.get("style", "") or ""
        if "display:none" in style:
            continue
        visible.append(p)
    if not visible:
        return None
    return max(visible, key=lambda p: p.length() if len(p) else 0)


def _sample(path) -> list[tuple[float, float]]:
    """Sample every segment of a Path -> list of (x_px, y_px) buffer coords.

    Coordinates are kept exactly as drawn in the SVG (no flip, no mirror).
    """
    if path is None or len(path) == 0:
        return []
    out: list[tuple[float, float]] = []
    for seg in path:
        if isinstance(seg, Line):
            samples = 1
        elif isinstance(seg, (CubicBezier, QuadraticBezier)):
            samples = SAMPLES_PER_CURVE
        else:
            samples = SAMPLES_PER_CURVE
        for i in range(samples):
            t = i / samples
            pt = seg.point(t)
            x_px = pt.real * SCALE
            y_px = pt.imag * SCALE
            out.append((round(x_px, 2), round(y_px, 2)))
    return out


def _format_poly(name: str, pts: list[tuple[float, float]], comment: str = "") -> str:
    if not pts:
        return f"{name} = []   # WARNING: empty (source SVG missing)\n"
    head = f"{name} = ["
    if comment:
        head += f"   # {comment}"
    head += "\n"
    body = ""
    for i in range(0, len(pts), 4):
        chunk = pts[i:i + 4]
        body += "    " + ", ".join(
            f"({x:6.2f}, {y:5.2f})" for x, y in chunk
        ) + ",\n"
    return head + body + "]\n"


def main() -> int:
    if not SRC_DIR.exists():
        print(f"# ERROR: source folder not found: {SRC_DIR}", file=sys.stderr)
        return 1

    nose_path = SRC_DIR / "nose.svg"
    nose_pts = _sample(_pick_visible_path(nose_path)) if nose_path.exists() else []
    if not nose_pts:
        print(f"# WARNING: no nose.svg in {SRC_DIR}", file=sys.stderr)

    out = []
    out.append('# ============================================================ polygon data ===\n')
    out.append('#\n')
    out.append('# Extracted 1:1 from the artist\'s SVGs (Pictures/proto/svg/face).\n')
    out.append('# SVG 200x100 mm → 64x32 buffer via uniform 0.32x scale on both axes.\n')
    out.append('# No flip, no mirror — polygon positions match the original artwork.\n')
    out.append('#\n\n')

    if nose_pts:
        out.append(_format_poly("NOSE", nose_pts, "shared, upper-right corner (artist's layout)"))
        out.append("\n")

    for emo, prefix in EMOTIONS_MAP.items():
        UPPER = emo.upper()
        label = EMOJI_LABEL[emo][1]
        out.append(f"# ---- {label} {'-' * (60 - len(label))}\n\n")

        eye_pts = _sample(_pick_visible_path(SRC_DIR / f"{prefix}_close_eye.svg"))
        mc_pts  = _sample(_pick_visible_path(SRC_DIR / f"{prefix}_close_mouth.svg"))
        mo_pts  = _sample(_pick_visible_path(SRC_DIR / f"{prefix}_open_mouth.svg"))

        out.append(_format_poly(f"{UPPER}_EYE", eye_pts))
        out.append("\n")
        out.append(_format_poly(f"{UPPER}_MOUTH_CLOSED", mc_pts, "closed mouth"))
        out.append("\n")
        out.append(_format_poly(f"{UPPER}_MOUTH_OPEN", mo_pts, "open mouth"))
        out.append("\n")

    out.append("\n# ============================================================ emotion table ===\n\n")
    out.append("# eye_polys = [EYE, NOSE] — NOSE is the static accent slot, identical for all emotions.\n")
    out.append("EMOTIONS = {\n")
    for emo in EMOTIONS_MAP:
        emoji, label = EMOJI_LABEL[emo]
        UPPER = emo.upper()
        nose_ref = ", NOSE" if nose_pts else ""
        out.append(
            f'    "{emo}": (\n'
            f'        "{emoji}", "{label}",\n'
            f'        [{UPPER}_EYE{nose_ref}],\n'
            f'        {UPPER}_MOUTH_CLOSED,\n'
            f'        {UPPER}_MOUTH_OPEN,\n'
            f'    ),\n'
        )
    out.append("}\n")

    sys.stdout.write("".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
