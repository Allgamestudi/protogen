#!/usr/bin/env python3
"""Сжимает SVG до 64x32 PNG (для LED-матрицы)."""
import argparse
import sys
from pathlib import Path

import cairosvg
from PIL import Image


def svg_to_64x32(src: Path, dst: Path, bg: str | None = None) -> None:
    png_bytes = cairosvg.svg2png(
        url=str(src),
        output_width=64,
        output_height=32,
    )
    tmp = dst.with_suffix(".tmp.png")
    tmp.write_bytes(png_bytes)

    img = Image.open(tmp).convert("RGBA")
    if bg:
        flat = Image.new("RGBA", img.size, bg)
        flat.alpha_composite(img)
        img = flat.convert("RGB")
    img.save(dst, optimize=True)
    tmp.unlink()


def main() -> int:
    p = argparse.ArgumentParser(description="SVG -> 64x32 PNG")
    p.add_argument("src", type=Path, help="входной .svg")
    p.add_argument("dst", type=Path, nargs="?", help="выходной .png (по умолчанию рядом с src)")
    p.add_argument("--bg", help="залить фон цветом, напр. '#000000' (иначе прозрачный)")
    args = p.parse_args()

    if not args.src.is_file():
        print(f"не найден файл: {args.src}", file=sys.stderr)
        return 1

    dst = args.dst or args.src.with_suffix(".64x32.png")
    svg_to_64x32(args.src, dst, args.bg)
    print(f"готово: {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
