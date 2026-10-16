"""Faces from the artist's 64x32 PNG masks (assets/emotion_masks_64x32).

At rest we show the masks as is. Anything that moves - mouth, blinks, morphing
between emotions, eye darts - goes through signed distance fields, so it works
for any shape (rings, split parts, 1px lines), not just polygons.
"""
import os

import numpy as np
from PIL import Image

PARTS = ("eye_open", "eye_closed", "mouth_closed", "mouth_open", "nose")
_FILES = {"eye_open": "open_eye", "eye_closed": "close_eye",
          "mouth_closed": "close_mouth", "mouth_open": "open_mouth"}
FAR = 16.0                   # fields are clipped to +-FAR pixels
OVERSHOOT_SWELL = 8.0        # pixels the shapes swell per unit of spring overshoot
MAX_THINNING = 0.35          # thin lines of the artwork must not vanish when a shape shrinks
BLINK_MELT = 3.0             # pixels the squashed open eye is eaten away by the end of a blink
LINE_SWAP = 1.2              # pixels a lid line is thinned by while the other one takes over
BLINK_LINE_LATE = 3.0        # the drawn lid line appears over the last third of the blink
ROUND_CUT = 0.5              # an "o" mouth loses this share of its length at the outer end
WIDE_SQUASH = 0.5            # an "s" mouth is this much flatter
FILL_RIM = 1.0               # pixels around the old pupil that are filled as well when it is moved
PUPIL_FADE = 6.0             # pixels a pupil shrinks by on the way to a face that has none
BLINK_MIN_HEIGHT = 0.1       # a fully closed eye keeps this share of its height
# The "close_eye" PNG counts as a drawn closed eye only when it differs enough
# from the open one; otherwise blinking squashes the open eye.
CLOSED_EYE_MAX_OVERLAP = 0.6
# The PNGs carry a faint haze around the shapes (1-3 % from resampling). It is
# invisible on the panel but would count as "edge"; pixels below FAINT are
# made empty and pixels above 1 - FAINT full.
FAINT = 0.06


def clean(cov):
    cov = np.asarray(cov, dtype=np.float32).copy()
    cov[cov < FAINT] = 0.0
    cov[cov > 1.0 - FAINT] = 1.0
    return cov


def _outline_points(cov):
    """Points on the outline of a coverage mask, with sub-pixel accuracy.

    Returns (points Nx2 as (y, x), half-width N). A partly covered pixel says
    where the edge crosses it: the fuller the pixel, the further the edge lies
    on its empty side. Hard steps (a full pixel next to an empty one) give the
    point between the two. Pixels of a line thinner than a pixel have no side:
    they are returned as centre points with half the line's width.
    """
    h, w = cov.shape
    pad = np.pad(cov, 1, mode="edge")
    gy = (pad[2:, 1:-1] - pad[:-2, 1:-1]) / 2.0
    gx = (pad[1:-1, 2:] - pad[1:-1, :-2]) / 2.0
    norm = np.hypot(gy, gx)
    partial = (cov > 1e-3) & (cov < 1.0 - 1e-3)
    ys, xs = np.nonzero(partial)
    c = cov[ys, xs]
    n = norm[ys, xs]
    sided = n > 0.15                         # the pixel has a clear inside and outside
    # unit normal pointing into the shape; the edge lies (c - 0.5) pixels from
    # the pixel centre, on the empty side when the pixel is more than half full
    safe = np.maximum(n, 1e-6)
    off = np.where(sided, c - 0.5, 0.0)
    py = ys - gy[ys, xs] / safe * off
    px = xs - gx[ys, xs] / safe * off
    # A faint pixel with no clear side is the fringe of an edge that its
    # neighbours already describe: it gets no point of its own.
    keep = sided | (c >= 0.2)
    pts = [np.stack([py, px], axis=1)[keep]]
    half = [np.where(sided, 0.0, c / 2.0)[keep]]

    full = cov >= 1.0 - 1e-3
    empty = cov <= 1e-3
    for dy, dx in ((0, 1), (1, 0)):          # hard steps, horizontal and vertical
        a_full, b_empty = full[:h - dy, :w - dx], empty[dy:, dx:]
        a_empty, b_full = empty[:h - dy, :w - dx], full[dy:, dx:]
        sy, sx = np.nonzero((a_full & b_empty) | (a_empty & b_full))
        pts.append(np.stack([sy + dy / 2.0, sx + dx / 2.0], axis=1))
        half.append(np.zeros(len(sy)))
    return np.concatenate(pts).astype(np.float32), np.concatenate(half).astype(np.float32)


def to_sdf(cov):
    """Coverage (0..1 per pixel) -> signed distance in pixels, clipped to +-FAR.

    The distance is measured to the outline with sub-pixel accuracy, which
    keeps mixed fields smooth. from_sdf(to_sdf(cov)) == cov exactly: partly
    covered pixels keep their value, full pixels stay full, empty stay empty.
    """
    cov = np.asarray(cov, dtype=np.float32)
    if (cov <= 1e-3).all():
        return np.full(cov.shape, FAR, dtype=np.float32)
    if (cov >= 1.0 - 1e-3).all():
        return np.full(cov.shape, -FAR, dtype=np.float32)
    pts, half = _outline_points(cov)
    ys, xs = np.indices(cov.shape)
    grid = np.stack([ys.ravel(), xs.ravel()], axis=1).astype(np.float32)
    dist = np.full(len(grid), np.inf, dtype=np.float32)
    for k in range(0, len(pts), 256):        # in chunks: keeps the temporary small
        chunk, hw = pts[k:k + 256], half[k:k + 256]
        d = np.sqrt(((grid[:, None, :] - chunk[None, :, :]) ** 2).sum(axis=2)) - hw[None, :]
        dist = np.minimum(dist, d.min(axis=1))
    dist = dist.reshape(cov.shape)
    sdf = np.where(cov >= 0.5, -np.abs(dist), np.abs(dist)).astype(np.float32)

    partial = (cov > 1e-3) & (cov < 1.0 - 1e-3)
    sdf[partial] = 0.5 - cov[partial]                       # exact at rest
    sdf[cov >= 1.0 - 1e-3] = np.minimum(sdf[cov >= 1.0 - 1e-3], -0.5)
    sdf[cov <= 1e-3] = np.maximum(sdf[cov <= 1e-3], 0.5)
    return np.clip(sdf, -FAR, FAR)


def from_sdf(sdf):
    """Signed distance -> coverage 0..1 with a one-pixel soft edge."""
    return np.clip(0.5 - sdf, 0.0, 1.0)


def shift(sdf, dx, dy):
    """Move a field by (dx, dy) pixels, fractions included. What comes in from
    beyond the edge is empty."""
    if dx == 0.0 and dy == 0.0:
        return sdf
    h, w = sdf.shape
    out = sdf
    for axis, delta in ((1, dx), (0, dy)):
        if delta == 0.0:
            continue
        whole = int(np.floor(delta))
        frac = float(delta - whole)
        n = out.shape[axis]

        def moved(k, src=out, axis=axis, n=n):
            res = np.full_like(src, FAR)
            if abs(k) >= n:
                return res
            a = [slice(None)] * 2
            b = [slice(None)] * 2
            a[axis] = slice(max(k, 0), n + min(k, 0))
            b[axis] = slice(max(-k, 0), n + min(-k, 0))
            res[tuple(a)] = src[tuple(b)]
            return res

        lo = moved(whole)
        out = lo if frac == 0.0 else lo * (1.0 - frac) + moved(whole + 1) * frac
    return out


def squash(sdf, centre_y, scale):
    """Squeeze a field vertically towards row centre_y (scale 1 = unchanged).

    Every output row takes the nearest shape of all the source rows that fall
    into it, so a thin line (a smiling eye) stays a line instead of dropping
    out between samples.
    """
    if scale >= 1.0:
        return sdf
    h = sdf.shape[0]
    scale = max(scale, 1e-3)
    out = np.full_like(sdf, FAR)
    for y in range(h):
        top = centre_y + (y - 0.5 - centre_y) / scale
        bottom = centre_y + (y + 0.5 - centre_y) / scale
        first, last = int(np.ceil(top)), int(np.floor(bottom))
        first, last = max(first, 0), min(last, h - 1)
        if first <= last:
            out[y] = sdf[first:last + 1].min(axis=0)
        else:                               # less than one source row: interpolate
            mid = (top + bottom) / 2.0
            lo = int(np.floor(mid))
            if 0 <= lo < h - 1:
                f = mid - lo
                out[y] = sdf[lo] * (1.0 - f) + sdf[lo + 1] * f
            elif lo == h - 1:
                out[y] = sdf[lo]
    return out


def pieces(mask):
    """Split a boolean mask into its connected pieces (8-neighbourhood)."""
    h, w = mask.shape
    seen = np.zeros(mask.shape, dtype=bool)
    out = []
    for y, x in zip(*np.nonzero(mask)):
        if seen[y, x]:
            continue
        piece = np.zeros(mask.shape, dtype=bool)
        stack = [(int(y), int(x))]
        seen[y, x] = True
        while stack:
            cy, cx = stack.pop()
            piece[cy, cx] = True
            for ny in range(max(cy - 1, 0), min(cy + 2, h)):
                for nx in range(max(cx - 1, 0), min(cx + 2, w)):
                    if mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
        out.append(piece)
    return out


def without_mouth(eye, mouth_closed, mouth_open):
    """Remove from an eye mask every piece that lies inside the mouth: an
    export slip in the artwork (the mouth layer left visible in an eye file)."""
    mouth = (mouth_closed > 0.3) | (mouth_open > 0.3)
    cleaned = eye.copy()
    for piece in pieces(eye > 0.0):
        if (piece & mouth).sum() > 0.5 * piece.sum():
            cleaned[piece] = 0.0
    return cleaned


class Face:
    """The five parts of one emotion as coverage masks and distance fields."""

    def __init__(self, masks):
        self.cov = {k: clean(masks[k]) for k in PARTS}
        for part in ("eye_open", "eye_closed"):
            self.cov[part] = without_mouth(self.cov[part], self.cov["mouth_closed"],
                                           self.cov["mouth_open"])
        self.sdf = {k: to_sdf(self.cov[k]) for k in PARTS}
        opened, closed = self.cov["eye_open"] > 0.5, self.cov["eye_closed"] > 0.5
        union = (opened | closed).sum()
        overlap = (opened & closed).sum() / union if union else 1.0
        self.closed_eye_drawn = bool(closed.any() and overlap < CLOSED_EYE_MAX_OVERLAP)
        # How far the open mouth reaches from the closed one: the lip-sync grows
        # the open shape out of the closed line over this distance.
        open_area = self.sdf["mouth_open"] < 0.5
        self.mouth_reach = (float((self.sdf["mouth_closed"] - self.sdf["mouth_open"])[open_area].max())
                            if open_area.any() else 0.0)
        # ...and how far the closed line lies outside the open shape: the open
        # shape starts that much fatter, so the two always touch.
        line = self.sdf["mouth_closed"] < 0.5
        self.mouth_gap = (max(0.0, float(self.sdf["mouth_open"][line].max()) + 0.5)
                          if line.any() else 0.0)
        rows = self.cov["eye_open"].sum(axis=1)
        self.eye_centre_y = (float((rows * np.arange(len(rows))).sum() / rows.sum())
                             if rows.sum() > 0 else self.cov["eye_open"].shape[0] / 2.0)
        cols = self.cov["eye_open"].sum(axis=0)
        self.eye_centre_x = (float((cols * np.arange(len(cols))).sum() / cols.sum())
                             if cols.sum() > 0 else self.cov["eye_open"].shape[1] / 4.0)
        # A drawn lid line for the shut eye (optional): the blink ends on it.
        self.blink_sdf = None
        self.blink_y = self.eye_centre_y
        self.set_blink(masks.get("eye_blink"))
        # Extent of the open mouth, for the mouth shapes of speech.
        ys, xs = np.nonzero(self.cov["mouth_open"] > 0.5)
        if len(xs):
            self.mouth_x0, self.mouth_x1 = float(xs.min()), float(xs.max())
            self.mouth_y = float(ys.mean())
        else:
            w = self.cov["mouth_open"].shape[1]
            self.mouth_x0, self.mouth_x1, self.mouth_y = w / 2.0, float(w - 1), 20.0

    pupil_sdf = None        # the pupil apart from the eyeball (anatomical eyes), or None

    def set_pupil(self, pupil):
        """Use this mask as the pupil: a dark hole in the eye that can move."""
        if pupil is not None and (clean(pupil) > 0.3).any():
            self.pupil_sdf = to_sdf(clean(pupil))

    def set_blink(self, blink):
        """Use this mask as the drawn lid line (None or empty: no line)."""
        if blink is None or not (clean(blink) > 0.3).any():
            return
        blink = clean(blink)
        self.blink_sdf = to_sdf(blink)
        rows = blink.sum(axis=1)
        self.blink_y = float((rows * np.arange(len(rows))).sum() / rows.sum())

    def static(self, part):
        """uint8 mask of one part, as the artwork has it."""
        return (self.cov[part] * 255.0 + 0.5).astype(np.uint8)


def read_mask(path, w, h):
    """A PNG as coverage 0..1, or None when it is missing or of the wrong size."""
    if not os.path.exists(path):
        return None
    img = Image.open(path).convert("L")
    if img.size != (w, h):
        return None
    return np.asarray(img, dtype=np.float32) / 255.0


def load_png_face(assets_dir, prefix, w, h, nose_dir=None, eye_file=None):
    """Read the PNG set of one emotion; None when it cannot be used.

    eye_file: take the eye from <prefix>_<eye_file>.png instead (another eye
    style on the same mouth); None is returned when there is no such file.

    Needed: <prefix>_open_eye and <prefix>_close_mouth. The rest has a stand-in:
    close_eye = the open eye, open_mouth = the closed one made fat, blink_eye
    (the drawn lid line) = none, nose = nose.png here or in nose_dir.
    """
    def read(name):
        return read_mask(os.path.join(assets_dir, name + ".png"), w, h)

    masks = {part: read(f"{prefix}_{suffix}") for part, suffix in _FILES.items()}
    if eye_file:
        masks["eye_open"] = masks["eye_closed"] = read(f"{prefix}_{eye_file}")
    if masks["eye_open"] is None or masks["mouth_closed"] is None:
        return None
    if masks["eye_closed"] is None:
        masks["eye_closed"] = masks["eye_open"]
    if masks["mouth_open"] is None:
        masks["mouth_open"] = from_sdf(to_sdf(clean(masks["mouth_closed"])) - 2.0)
    nose = read("nose")
    if nose is None and nose_dir:
        nose = read_mask(os.path.join(nose_dir, "nose.png"), w, h)
    masks["nose"] = nose if nose is not None else np.zeros((h, w), dtype=np.float32)
    masks["eye_blink"] = read(f"{prefix}_blink_eye")
    return Face(masks)


def shrink(sdf, cx, cy, scale):
    """Scale a shape down towards (cx, cy); scale 1 = unchanged."""
    if scale >= 1.0:
        return sdf
    scale = max(scale, 0.05)
    return squash(squash(sdf.T, cx, scale).T, cy, scale) * scale


def _mix(a, b, part, blend):
    if a is b or blend <= 0.0:
        return a.sdf[part]
    if blend >= 1.0:
        return b.sdf[part]
    return a.sdf[part] * (1.0 - blend) + b.sdf[part] * blend


def _pupil_field(a, b, blend):
    """Field of the pupil between two faces; None when neither has one."""
    pa, pb = a.pupil_sdf, b.pupil_sdf
    if pa is None and pb is None:
        return None
    if a is b or blend <= 0.0:
        return pa
    if blend >= 1.0:
        return pb
    # a face without a pupil: the other one's pupil shrinks away / grows in
    if pa is None:
        return pb + (1.0 - blend) * PUPIL_FADE
    if pb is None:
        return pa + blend * PUPIL_FADE
    return pa * (1.0 - blend) + pb * blend


def _eye_field(a, b, blend, blink, eye_dx, eye_dy, eye_grow, eye_scale=1.0,
               pupil_dx=0.0, pupil_dy=0.0, pupil=True, pupil_grow=0.0):
    """Distance field of the eye for one frame; blend may run past 1.

    pupil_dx/dy move the pupil inside the eye (the gaze); pupil=False leaves
    the pupil out and gives the whole eyeball."""
    # The transition is a spring: `blend` runs past 1 and swings back. Mixing
    # two fields beyond 1 tears shapes apart, so the mix stops at 1 and the
    # overshoot makes the new shape swell a little instead.
    swell = max(0.0, blend - 1.0) * OVERSHOOT_SWELL
    blend = min(max(blend, 0.0), 1.0)
    eye = _mix(a, b, "eye_open", blend)
    if pupil:
        hole = _pupil_field(a, b, blend)
        if hole is not None:
            # cut the pupil out before the lids act on the eye: a blink covers it too
            eye = np.maximum(eye, pupil_grow - shift(hole, pupil_dx, pupil_dy))
    if eye_scale < 1.0:
        eye = shrink(eye, a.eye_centre_x * (1.0 - blend) + b.eye_centre_x * blend,
                     a.eye_centre_y * (1.0 - blend) + b.eye_centre_y * blend, eye_scale)
    if blink > 0.0:
        if a.blink_sdf is not None and b.blink_sdf is not None:
            # The lid comes down onto the drawn line: the open eye is squashed
            # towards it and melts away, the line shows up for the last part.
            # between two emotions one line thins out while the other thickens:
            # mixing the fields of two thin lines in different places leaves nothing
            line = (a.blink_sdf if (a is b or blend <= 0.0) else b.blink_sdf if blend >= 1.0
                    else np.minimum(a.blink_sdf + blend * LINE_SWAP, b.blink_sdf + (1.0 - blend) * LINE_SWAP))
            centre = a.blink_y * (1.0 - blend) + b.blink_y * blend
            height = 1.0 - (1.0 - BLINK_MIN_HEIGHT) * blink
            eye = squash(eye, centre, height)
            # squashing keeps the old depth inside the shape; make it as shallow
            # as the flat shape really is, so the melting below can finish it off
            eye = np.where(eye < 0.0, eye * height, eye) + BLINK_MELT * max(0.0, (blink - 0.6) / 0.4) ** 2
            eye = np.minimum(eye, line + (1.0 - blink) * BLINK_LINE_LATE)
        elif a.closed_eye_drawn and b.closed_eye_drawn:
            eye = eye * (1.0 - blink) + _mix(a, b, "eye_closed", blend) * blink
        else:
            centre = a.eye_centre_y * (1.0 - blend) + b.eye_centre_y * blend
            eye = squash(eye, centre, 1.0 - (1.0 - BLINK_MIN_HEIGHT) * blink)
    eye = shift(eye, eye_dx, eye_dy)
    grow = max(eye_grow, -MAX_THINNING) + swell
    return eye - grow if grow else eye


def pupil_holes(a, b, blend=0.0, blink=0.0, eye_dx=0.0, eye_dy=0.0, eye_grow=0.0,
                eye_scale=1.0, gazes=((0.0, 0.0),)):
    """Where the pupil shows for each gaze in `gazes`: coverage 0..1 of the part
    of the eye it darkens (after lids, blink and all). Used to move the pupil
    in a finished picture: fill the old place, darken the new one."""
    whole = from_sdf(_eye_field(a, b, blend, blink, eye_dx, eye_dy, eye_grow, eye_scale, pupil=False))
    # The first one is "where it is now" and gets a rim (FILL_RIM): the picture
    # it is used on has been resampled, and a fill of the exact size would
    # leave the outline of the old pupil behind.
    return [np.clip(whole - from_sdf(_eye_field(a, b, blend, blink, eye_dx, eye_dy, eye_grow,
                                               eye_scale, gx, gy, pupil_grow=FILL_RIM if n == 0 else 0.0)),
                    0.0, 1.0).astype(np.float32)
            for n, (gx, gy) in enumerate(gazes)]


def eye_halo(a, b, blend=0.0, blink=0.0, eye_dx=0.0, eye_dy=0.0, eye_grow=0.0, margin=3.0,
             eye_scale=1.0):
    """Soft mask (0..1) of the eye with `margin` pixels around it: what has to
    be picked up to move the eye, with its glow and highlight, somewhere else."""
    return from_sdf(_eye_field(a, b, blend, blink, eye_dx, eye_dy, eye_grow, eye_scale, pupil=False)
                    - margin).astype(np.float32)


def render(a, b, blend=0.0, mouth=0.0, blink=0.0, eye_dx=0.0, eye_dy=0.0, nose_dy=0.0,
           eye_grow=0.0, mouth_grow=0.0, mouth_blend=None, eye_scale=1.0,
           mouth_round=0.0, mouth_wide=0.0, pupil_dx=0.0, pupil_dy=0.0):
    """One frame of the face as coverage 0..1 (float32, H x W).

    a, b      Face objects: the emotion we come from and the one we go to
    blend     0 = a, 1 = b; may overshoot a little (springy transition)
    mouth     0 = closed, 1 = open
    blink     0 = eye open, 1 = eye closed. Uses the drawn closed eye when the
              artwork has a real one, otherwise squashes the open eye flat
    eye_dx/dy shift of the eye in pixels; nose_dy shift of the nose
    eye_grow, mouth_grow   pixels the shape gets fatter (negative: thinner)
    mouth_blend   own blend for the mouth, so it can follow the eye with a delay
    eye_scale     below 1 the eye shrinks towards its centre
    mouth_round   0..1 "o": the open mouth gets shorter and rounder
    mouth_wide    0..1 "s": the open mouth gets flat
    pupil_dx/dy   shift of the pupil inside the eye, for faces that have one
    """
    eye = _eye_field(a, b, blend, blink, eye_dx, eye_dy, eye_grow, eye_scale, pupil_dx, pupil_dy)

    mb = blend if mouth_blend is None else mouth_blend
    mouth_swell = max(0.0, mb - 1.0) * OVERSHOOT_SWELL
    mb = min(max(mb, 0.0), 1.0)
    # Lip-sync: the open mouth grows out of the closed line. A plain mix of the
    # two fields would show a thin line and a detached blob half-way.
    closed = _mix(a, b, "mouth_closed", mb)
    if mouth <= 0.0:
        mouth_sdf = closed
    elif mouth >= 1.0:
        mouth_sdf = _mix(a, b, "mouth_open", mb)
    else:
        reach = a.mouth_reach * (1.0 - mb) + b.mouth_reach * mb
        gap = a.mouth_gap * (1.0 - mb) + b.mouth_gap * mb
        fat_open = _mix(a, b, "mouth_open", mb) - (1.0 - mouth) * gap
        grown = np.maximum(fat_open, closed - mouth * reach)    # open shape near the line
        mouth_sdf = np.minimum(grown, closed + mouth)           # plus the fading line
    if mouth > 0.0 and (mouth_round > 0.0 or mouth_wide > 0.0):
        if mouth_wide > 0.0:
            y = a.mouth_y * (1.0 - mb) + b.mouth_y * mb
            mouth_sdf = squash(mouth_sdf, y, 1.0 - WIDE_SQUASH * mouth_wide * mouth)
        if mouth_round > 0.0:
            x0 = a.mouth_x0 * (1.0 - mb) + b.mouth_x0 * mb
            x1 = a.mouth_x1 * (1.0 - mb) + b.mouth_x1 * mb
            cut = x0 + (x1 - x0) * ROUND_CUT * mouth_round * mouth
            columns = np.arange(mouth_sdf.shape[1], dtype=np.float32)[None, :]
            mouth_sdf = np.maximum(mouth_sdf, cut - columns) - 0.6 * mouth_round * mouth
    grow = max(mouth_grow, -MAX_THINNING) + mouth_swell
    if grow:
        mouth_sdf = mouth_sdf - grow

    nose = shift(_mix(a, b, "nose", min(max(blend, 0.0), 1.0)), 0.0, nose_dy)
    # union of shapes = minimum of their distances
    return from_sdf(np.minimum(np.minimum(eye, mouth_sdf), nose)).astype(np.float32)
