"""Draws the artwork the original set does not have, in the same style and layout.

    python scripts/make_extra_art.py            write the missing files
    python scripts/make_extra_art.py --force    redraw the files this script made before

Into assets/emotion_masks_64x32/:
  sleepy / love / evil      open eye, and the mouths copied from a drawn emotion
  <emotion>_real_ball.png   the eye of the "anatomical" style (setting eye_style): the eyeball
  <emotion>_real_pupil.png  with its lids, and the pupil apart from it, so that it can move.
  <emotion>_real_eye.png    Both together, as a preview; the firmware uses it only if the two
                            separate files are missing
  <emotion>_blink_eye.png   a drawn closed eye for all nine (lid line of its own shape)

The artist's files are never touched. Any of these PNGs can be redrawn by hand:
white on black, 64 x 32, eye on the left, mouth on the right.
"""
import math
import os
import shutil
import sys

from PIL import Image, ImageChops, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ART = os.path.join(ROOT, "assets", "emotion_masks_64x32")
W, H, SS = 64, 32, 8                       # drawn 8x larger, then averaged down
LINE = 1.7                                 # width of the lid lines, panel pixels
MARK = "made by make_extra_art.py"         # PNG text field that marks our own files

# file prefix of each emotion, and whose mouth the new three use
PREFIX = {"angry": "angry", "bad": "bad", "happy": "happy", "sad": "sad",
          "scared": "испуг", "surprised": "удивление",
          "sleepy": "sleepy", "love": "love", "evil": "evil"}
MOUTH_FROM = {"sleepy": "sad", "love": "happy", "evil": "angry"}


def canvas():
    img = Image.new("L", (W * SS, H * SS), 0)
    return img, ImageDraw.Draw(img)


def px(points):
    return [(x * SS, y * SS) for x, y in points]


def finish(img):
    return img.resize((W, H), Image.BOX)


def stroke(points, width=LINE):
    """A round-ended line through the points."""
    img, d = canvas()
    pts = px(points)
    d.line(pts, fill=255, width=int(width * SS), joint="curve")
    r = width * SS / 2.0
    for x, y in (pts[0], pts[-1]):
        d.ellipse([x - r, y - r, x + r, y + r], fill=255)
    return finish(img)


def arc(x0, x1, y_ends, y_mid, n=24):
    """Points of a parabola from (x0, y_ends) over (middle, y_mid) to (x1, y_ends)."""
    out = []
    for i in range(n + 1):
        u = i / n
        out.append((x0 + (x1 - x0) * u, y_ends + (y_mid - y_ends) * 4 * u * (1 - u)))
    return out


# open eyes
def eye_sleepy():
    """Half-shut: the lower part of a bowl under a flat, slightly drooping lid."""
    img, d = canvas()
    d.ellipse(px([(8, 3.5), (25, 16.5)]), fill=255)
    d.polygon(px([(0, 0), (64, 0), (64, 11.2), (0, 9.8)]), fill=0)      # the lid cuts the top off
    return finish(img)


def eye_love():
    """A heart."""
    img, d = canvas()
    cx, cy, s = 16.5, 9.6, 0.56
    pts = []
    for i in range(200):
        t = 2 * math.pi * i / 200
        x = 16 * math.sin(t) ** 3
        y = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
        pts.append((cx + x * s, cy - y * s))
    d.polygon(px(pts), fill=255)
    return finish(img)


def eye_evil():
    """A sharp bowl under a steep brow line."""
    img, d = canvas()
    d.ellipse(px([(6.5, 0.5), (25.5, 17.0)]), fill=255)
    d.polygon(px([(0, 0), (64, 0), (64, 26.6), (27, 11.8), (6, 3.4), (0, 1.0)]), fill=0)
    eye = finish(img)
    brow = stroke([(6.5, 1.6), (26.5, 9.6)], 1.3)
    return ImageChops.lighter(eye, brow)


OPEN_EYES = {"sleepy": eye_sleepy, "love": eye_love, "evil": eye_evil}


# anatomical eyes
# The panels sit on the sides of the head with the nose towards the middle
# (the right edge of the picture). A pupil in the middle of the eye looks
# sideways out of the head; moved towards the nose it looks ahead.
FORWARD = 2.5      # leaves the pupil room to look further towards the nose


def eyeball(ball=(6.5, 1.0, 25.5, 17.0), upper=None, lower=None, pupil=None, brow=None,
            heart=False, part="eye"):
    """An eyeball with lids and a dark pupil.

    ball   the open eye as an ellipse (x0, y0, x1, y1)
    upper  upper lid: the eye is cut off above the line through these points
    lower  lower lid: cut off below
    pupil  (cx, cy, rx, ry) of the dark pupil: narrow = slit, round = wide open.
           cx is given for an eye looking straight out of the panel; the pupil
           is drawn FORWARD pixels nearer the nose, so the face looks ahead.
           cy is the middle of the eyeball, not of what the lids leave open: a
           lowered lid covers the top of the pupil, the eye still looks ahead
    brow   a stroke above the eye
    part   "eye" = all of it; "ball" = without the pupil; "pupil" = the pupil
           alone, whole, white on black. The firmware keeps the last two apart
           so that the pupil can move under the lids.
    """
    img, d = canvas()
    if part != "pupil":
        d.ellipse(px([ball[:2], ball[2:]]), fill=255)
        if upper:
            d.polygon(px([(0, -40)] + list(upper) + [(64, -40)]), fill=0)
        if lower:
            d.polygon(px([(0, 80)] + list(lower) + [(64, 80)]), fill=0)
    if pupil and part != "ball":
        ink = 255 if part == "pupil" else 0
        cx, cy = pupil[0] + FORWARD, pupil[1]
        if heart:
            size = pupil[2]
            pts = []
            for i in range(120):
                t = 2 * math.pi * i / 120
                x = 16 * math.sin(t) ** 3
                y = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
                pts.append((cx + x * size / 16.0, cy - y * size / 16.0))
            d.polygon(px(pts), fill=ink)
        else:
            rx, ry = pupil[2], pupil[3]
            d.ellipse(px([(cx - rx, cy - ry), (cx + rx, cy + ry)]), fill=ink)
    eye = finish(img)
    if brow and part != "pupil":
        eye = ImageChops.lighter(eye, stroke(brow, 1.3))
    return eye


REAL_EYES = {
    # frowning lid, heavy brow right on it, narrow slit
    "angry": dict(upper=[(0, 2.5), (6, 4.5), (27, 12.5), (64, 26)],
                  pupil=(16.5, 10.6, 1.1, 4.0), brow=[(7.0, 2.6), (26.0, 9.8)]),
    # half-shut
    "bad": dict(upper=[(0, 7.2), (64, 10.8)], pupil=(16.5, 10.6, 1.3, 3.8),
                brow=[(8.0, 4.6), (25.0, 6.4)]),
    # the cheek pushes the lower lid up
    "happy": dict(ball=(6.5, 3.0, 25.5, 20.0), lower=arc(6.5, 25.5, 12.5, 8.0),
                  pupil=(15.5, 6.6, 1.4, 2.2)),
    # the outer corner hangs, the inner end of the brow is raised
    "sad": dict(upper=[(0, 10.2), (6, 9.0), (27, 3.0), (64, -8)],
                pupil=(16.0, 9.8, 1.9, 3.2), brow=[(7.0, 7.4), (25.5, 1.8)]),
    # wide open, the pupil shrunk to a dot
    "scared": dict(ball=(5.5, 0.5, 24.5, 18.5), pupil=(15.0, 9.5, 1.4, 2.0)),
    # round, the pupil wide
    "surprised": dict(ball=(7.5, 1.0, 24.5, 18.0), pupil=(15.5, 9.5, 2.5, 2.5)),
    # the lid has almost closed
    "sleepy": dict(upper=[(0, 10.2), (64, 12.2)], pupil=(16.0, 11.6, 1.7, 3.6)),
    # a heart for a pupil
    "love": dict(ball=(6.5, 1.0, 25.5, 18.0), pupil=(15.0, 9.3, 4.6, 0), heart=True),
    # sharp, with a slit
    "evil": dict(ball=(6.5, 0.5, 25.5, 17.0), upper=[(0, 1.0), (6, 3.4), (27, 11.8), (64, 26.6)],
                 pupil=(16.6, 10.8, 1.3, 4.0), brow=[(6.5, 1.6), (26.5, 9.6)]),
}

# closed eyes
BLINK = {
    "angry":     lambda: stroke([(8.5, 9.0), (24.5, 13.5)]),                    # slanted, frowning
    "bad":       lambda: stroke(arc(8.5, 24.5, 10.5, 12.5)),                    # lazy lid
    "happy":     lambda: stroke(arc(8.5, 24.5, 11.0, 6.5)),                     # ^ smiling eye
    "sad":       lambda: stroke([(8.5, 13.5), (15, 12.6), (24.5, 10.0)]),       # outer end hangs
    "scared":    lambda: stroke([(9.5, 6.5), (22.5, 11.5), (9.5, 16.5)]),       # > squeezed shut
    "surprised": lambda: stroke([(9.5, 10.5), (23.5, 10.5)]),                   # flat
    "sleepy":    lambda: stroke(arc(8.5, 24.5, 11.0, 14.5)),                    # u sleeping
    "love":      lambda: stroke(arc(8.5, 24.5, 12.0, 7.0)),                     # ^
    "evil":      lambda: stroke([(7.0, 6.0), (25.5, 13.0)], 2.0),               # steep slit
}


def ours(path):
    try:
        return Image.open(path).info.get("Comment") == MARK
    except Exception:
        return False


def save(img, name, force):
    from PIL.PngImagePlugin import PngInfo
    path = os.path.join(ART, name)
    if os.path.exists(path) and not (force and ours(path)):
        return False
    info = PngInfo()
    info.add_text("Comment", MARK)
    img.convert("RGB").save(path, pnginfo=info)
    return True


def main():
    force = "--force" in sys.argv
    made = []
    for eid, draw in OPEN_EYES.items():
        eye = draw()
        for suffix in ("open_eye", "close_eye"):
            if save(eye, f"{PREFIX[eid]}_{suffix}.png", force):
                made.append(f"{PREFIX[eid]}_{suffix}.png")
        donor = PREFIX[MOUTH_FROM[eid]]
        for suffix in ("close_mouth", "open_mouth"):
            dst = os.path.join(ART, f"{PREFIX[eid]}_{suffix}.png")
            if not os.path.exists(dst):
                shutil.copyfile(os.path.join(ART, f"{donor}_{suffix}.png"), dst)
                made.append(os.path.basename(dst))
    for eid, shape in REAL_EYES.items():
        for part in ("eye", "ball", "pupil"):
            name = f"{PREFIX[eid]}_real_{part}.png"
            if save(eyeball(part=part, **shape), name, force):
                made.append(name)
    for eid, draw in BLINK.items():
        if save(draw(), f"{PREFIX[eid]}_blink_eye.png", force):
            made.append(f"{PREFIX[eid]}_blink_eye.png")
    print(f"written: {len(made)}", *made, sep="\n  ")


if __name__ == "__main__":
    main()
