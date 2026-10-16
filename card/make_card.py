"""Business card of Wild Proto Lab: 90 x 50 mm, QR code to the Telegram channel.

    python card/make_card.py

writes into this folder, for each look (dark, light):
    card_<look>.png        one card, to look at or to send
    card_<look>_A4.pdf     ten cards on an A4 sheet with cut marks, to print at 100 % scale

Change the texts below (or replace logo.png) and run again.
Needs Pillow, NumPy and segno (pip install segno).
"""
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
import segno

LINK = "https://t.me/Wild_Proto_Lab"
TITLE = "WILD PROTO LAB"
TAGLINE = ("Протогены", "LED-визоры", "электроника")      # one line each, beside the logo
LOGO = "logo.png"               # square picture in this folder
SHOWN_LINK = "t.me/Wild_Proto_Lab"
SCAN_ME = "Telegram"

HERE = Path(__file__).resolve().parent
DPI = 600
CARD_MM = (90.0, 50.0)
BLEED_MM = 2.0                  # the background runs this far past the cut line
FONT_BOLD = r"C:\Windows\Fonts\segoeuib.ttf"
FONT_SEMI = r"C:\Windows\Fonts\seguisb.ttf"
FONT = r"C:\Windows\Fonts\segoeui.ttf"
LOOKS = {
    "dark":  {"bg": (13, 15, 20), "text": (240, 242, 247), "dim": (150, 157, 175),
              "accent": (255, 45, 45), "led_off": (30, 33, 43)},
    "light": {"bg": (255, 255, 255), "text": (20, 22, 28), "dim": (95, 100, 115),
              "accent": (215, 25, 25), "led_off": (232, 234, 240)},
}


def mm(v):
    return int(round(v * DPI / 25.4))


def logo_tile(size, look):
    """The logo as a square with rounded corners, on the card's background."""
    logo = Image.open(HERE / LOGO).convert("RGBA")
    logo = Image.alpha_composite(Image.new("RGBA", logo.size, look["bg"] + (255,)), logo).convert("RGB")
    side = min(logo.size)
    left, top = (logo.width - side) // 2, (logo.height - side) // 2
    logo = logo.crop((left, top, left + side, top + side)).resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size * 4 - 1, size * 4 - 1), radius=mm(2.2) * 4, fill=255)
    tile = Image.new("RGB", (size, size), look["bg"])
    tile.paste(logo, (0, 0), mask.resize((size, size), Image.LANCZOS))
    return tile


def qr_tile(size):
    """Black on white with the quiet zone, `size` pixels square, crisp modules."""
    qr = segno.make(LINK, error="q", micro=False)
    border = 3
    modules = qr.symbol_size(border=border)[0]
    scale = size // modules
    matrix = np.array([list(row) for row in qr.matrix_iter(border=border)], dtype=bool)
    tile = Image.fromarray(np.where(matrix, 0, 255).astype(np.uint8)).resize(
        (modules * scale, modules * scale), Image.NEAREST)
    return tile.convert("RGB")


def spaced(draw, xy, text, font, fill, spacing):
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + spacing
    return x


def spaced_width(draw, text, font, spacing):
    return sum(draw.textlength(ch, font=font) + spacing for ch in text) - spacing


def make_card(look, bleed=True):
    b = mm(BLEED_MM) if bleed else 0
    W, H = mm(CARD_MM[0]) + 2 * b, mm(CARD_MM[1]) + 2 * b
    img = Image.new("RGB", (W, H), look["bg"])
    d = ImageDraw.Draw(img)
    pad = b + mm(5.0)

    # right: the QR code on a white tile
    qr = qr_tile(mm(29.0))
    tile = qr.width + mm(2.0)
    tx, ty = W - pad - tile, b + mm(5.0)
    d.rounded_rectangle((tx, ty, tx + tile, ty + tile), radius=mm(1.6), fill=(255, 255, 255),
                        outline=look["led_off"], width=mm(0.15))
    img.paste(qr, (tx + (tile - qr.width) // 2, ty + (tile - qr.height) // 2))
    small = ImageFont.truetype(FONT_SEMI, mm(2.5))
    label_w = d.textlength(SCAN_ME, font=small)
    d.text((tx + (tile - label_w) / 2, ty + tile + mm(1.2)), SCAN_ME, font=small, fill=look["dim"])

    # left: name, what it is, the link
    left_w = tx - pad - mm(4.0)
    size = mm(6.2)
    while True:                                     # the largest title that fits
        title = ImageFont.truetype(FONT_BOLD, size)
        spacing = size * 0.06
        if spaced_width(d, TITLE, title, spacing) <= left_w:
            break
        size -= 2
    y = b + mm(4.6)
    spaced(d, (pad, y), TITLE, title, look["text"], spacing)
    y += int(size * 1.42)
    d.rectangle((pad, y, pad + mm(12.0), y + mm(0.7)), fill=look["accent"])

    # the logo, and beside it what the lab does
    side = mm(20.5)
    ly = b + mm(17.2)
    img.paste(logo_tile(side, look), (pad, ly))
    d.rounded_rectangle((pad, ly, pad + side, ly + side), radius=mm(2.2), outline=look["led_off"], width=mm(0.15))
    tx2 = pad + side + mm(3.5)
    size = mm(3.3)
    while True:
        tag = ImageFont.truetype(FONT, size)
        if max(d.textlength(line, font=tag) for line in TAGLINE) <= tx - mm(3.0) - tx2:
            break
        size -= 2
    step = int(size * 1.55)
    ty2 = ly + (side - step * len(TAGLINE)) // 2 + int(size * 0.1)
    for n, line in enumerate(TAGLINE):
        d.ellipse((tx2, ty2 + n * step + size * 0.62, tx2 + mm(0.9), ty2 + n * step + size * 0.62 + mm(0.9)),
                  fill=look["accent"])
        d.text((tx2 + mm(2.2), ty2 + n * step), line, font=tag, fill=look["dim"])

    link = ImageFont.truetype(FONT_SEMI, mm(3.6))
    d.text((pad, H - b - mm(5.0) - mm(4.2)), SHOWN_LINK, font=link, fill=look["accent"])
    return img


def a4_sheet(card):
    """Ten cards (2 x 5) with their bleeds touching, cut marks in the margins."""
    W, H = mm(210.0), mm(297.0)
    sheet = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(sheet)
    b = mm(BLEED_MM)
    cw, ch = mm(CARD_MM[0]), mm(CARD_MM[1])
    x0 = (W - 2 * (cw + 2 * b)) // 2
    y0 = (H - 5 * (ch + 2 * b)) // 2
    xs, ys = set(), set()
    for row in range(5):
        for col in range(2):
            x, y = x0 + col * (cw + 2 * b), y0 + row * (ch + 2 * b)
            sheet.paste(card, (x, y))
            xs.update((x + b, x + b + cw))
            ys.update((y + b, y + b + ch))
    mark, gap, line = mm(4.0), mm(1.0), max(1, mm(0.12))
    top, bottom = y0, y0 + 5 * (ch + 2 * b)
    left, right = x0, x0 + 2 * (cw + 2 * b)
    for x in xs:
        d.line((x, top - gap - mark, x, top - gap), fill=(0, 0, 0), width=line)
        d.line((x, bottom + gap, x, bottom + gap + mark), fill=(0, 0, 0), width=line)
    for y in ys:
        d.line((left - gap - mark, y, left - gap, y), fill=(0, 0, 0), width=line)
        d.line((right + gap, y, right + gap + mark, y), fill=(0, 0, 0), width=line)
    return sheet


def main():
    for name, look in LOOKS.items():
        make_card(look, bleed=False).save(HERE / f"card_{name}.png", dpi=(DPI, DPI))
        a4_sheet(make_card(look)).save(HERE / f"card_{name}_A4.pdf", resolution=DPI)
        print("written:", f"card_{name}.png", f"card_{name}_A4.pdf")


if __name__ == "__main__":
    main()
