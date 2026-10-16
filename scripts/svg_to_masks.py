"""Rasterize the artist's 64x32 SVG face components into binary PNG masks.

The artist now ships per-component SVGs already authored at the target
matrix resolution (Pictures/proto/svg/face/64x32/svg). Each file is a
64x32 canvas containing a black background rectangle plus one or more
white shapes (VTracer output).  We just need to render them and keep
only the lit pixels - emotions.py loads the result as a binary mask.

Layout under assets/emotion_masks_64x32/ matches the source filenames
1:1, so emotions.py can pick up the new artwork without code changes.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image


SRC_DIR  = Path(r"C:\Users\User\Pictures\proto\svg\face\64x32\svg")
DST_DIR  = Path(__file__).resolve().parent.parent / "assets" / "emotion_masks_64x32"
INKSCAPE = Path(r"C:\Program Files\Inkscape\bin\inkscape.exe")

OUT_W, OUT_H = 64, 32
SUPER        = 4      # supersample factor for smoother edges before binarise
THRESHOLD    = 128    # pixels >= this -> lit in the output mask


def render_one(svg: Path, out: Path) -> None:
    """SVG -> supersampled PNG -> LANCZOS -> binarise -> save."""
    tmp = out.with_name(f"_tmp_{out.stem}.png")
    subprocess.run(
        [
            str(INKSCAPE),
            "--export-type=png",
            f"--export-width={OUT_W * SUPER}",
            f"--export-height={OUT_H * SUPER}",
            "--export-area-page",
            "--export-background=#000000",
            "--export-background-opacity=1.0",
            f"--export-filename={tmp}",
            str(svg),
        ],
        check=True,
        capture_output=True,
    )
    img = Image.open(tmp).convert("L").resize((OUT_W, OUT_H), Image.LANCZOS)
    arr = np.asarray(img, dtype=np.uint8)
    mask = (arr >= THRESHOLD).astype(np.uint8) * 255
    Image.fromarray(mask, mode="L").save(out)
    tmp.unlink()


def main() -> int:
    if not INKSCAPE.exists():
        print(f"[X] Inkscape not found at {INKSCAPE}", file=sys.stderr)
        return 1
    if not SRC_DIR.exists():
        print(f"[X] SVG folder not found: {SRC_DIR}", file=sys.stderr)
        return 1
    DST_DIR.mkdir(parents=True, exist_ok=True)

    ok = skipped = 0
    for svg in sorted(SRC_DIR.glob("*.svg")):
        # Skip stray typo file like "happy_щзут_mouth.svg" (Russian-layout slip).
        if "щзут" in svg.name:
            print(f"[skip] {svg.name} (typo)")
            skipped += 1
            continue
        out = DST_DIR / f"{svg.stem}.png"
        try:
            render_one(svg, out)
            print(f"[OK] {svg.name:35s} -> {out.name}")
            ok += 1
        except Exception as e:
            print(f"[!]  {svg.name}: {e}")
            skipped += 1

    print(f"\n{ok} rendered, {skipped} skipped — output in {DST_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
