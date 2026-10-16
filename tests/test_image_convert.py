"""Checks for the upload converter; needs only NumPy and Pillow.

Run from the project root: python -B -m unittest discover -s tests -v
"""
from io import BytesIO
from pathlib import Path
import sys
import unittest

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pi"))
import image_convert as ic  # noqa: E402

W, H = 64, 32


def encode(img, fmt, **kw):
    out = BytesIO()
    img.save(out, fmt, **kw)
    return out.getvalue()


def circle_on_transparent():
    img = Image.new("RGBA", (200, 100), (255, 255, 255, 0))
    ImageDraw.Draw(img).ellipse((60, 10, 140, 90), fill=(255, 0, 0, 255))
    return img


class ConvertUploadTests(unittest.TestCase):
    def test_transparent_png_is_black_not_white(self):
        frames, info = ic.convert_upload(encode(circle_on_transparent(), "PNG"), W, H)
        self.assertFalse(info["animated"])
        frame = frames[0][0]
        self.assertEqual(frame.shape, (H, W, 3))
        self.assertEqual(frame[1, 1].tolist(), [0, 0, 0])
        self.assertEqual(frame[16, 32].tolist(), [255, 0, 0])

    def test_palette_transparency_is_black(self):
        img = Image.new("P", (200, 100), 0)
        img.putpalette([255, 255, 255, 255, 0, 0] + [0] * 750)
        ImageDraw.Draw(img).ellipse((60, 10, 140, 90), fill=1)
        frames, _ = ic.convert_upload(encode(img, "PNG", transparency=0), W, H)
        self.assertEqual(frames[0][0][1, 1].tolist(), [0, 0, 0])

    def test_half_transparent_is_dimmed(self):
        frames, _ = ic.convert_upload(encode(Image.new("LA", (64, 32), (255, 128)), "PNG"), W, H)
        self.assertTrue(120 <= int(frames[0][0][5, 5, 0]) <= 135)

    def test_small_picture_is_enlarged_with_hard_pixels(self):
        img = Image.new("RGB", (16, 8), (0, 0, 0))
        img.putpixel((0, 0), (0, 255, 0))
        frame = ic.convert_upload(encode(img, "PNG"), W, H)[0][0][0]
        self.assertTrue((frame[0:4, 0:4] == [0, 255, 0]).all())     # one pixel -> 4x4 block
        self.assertEqual(frame[0, 4].tolist(), [0, 0, 0])

    def test_exact_size_is_untouched(self):
        rng = np.random.default_rng(1)
        arr = rng.integers(0, 256, (H, W, 3), dtype=np.uint8)
        frame = ic.convert_upload(encode(Image.fromarray(arr), "PNG"), W, H)[0][0][0]
        self.assertTrue((frame == arr).all())

    def test_tall_picture_is_centred(self):
        raw = encode(Image.new("RGB", (100, 200), (9, 9, 9)), "PNG")
        frame = ic.convert_upload(raw, W, H)[0][0][0]
        lit = np.where(frame[0].sum(axis=1) > 0)[0]
        self.assertEqual((int(lit[0]), int(lit[-1])), (24, 39))      # 16 columns in the middle

    def test_gif_frames_delays_and_transparency(self):
        frames = []
        for i in range(4):
            f = Image.new("RGBA", (128, 64), (0, 0, 0, 0))
            ImageDraw.Draw(f).rectangle((i * 30, 20, i * 30 + 20, 40), fill=(0, 0, 255, 255))
            frames.append(f)
        raw = encode(frames[0], "GIF", save_all=True, append_images=frames[1:], duration=0,
                     loop=0, disposal=2, transparency=0)
        out, info = ic.convert_upload(raw, W, H)
        self.assertTrue(info["animated"])
        self.assertEqual(len(out), 4)
        self.assertEqual([d for _, d in out], [0.1] * 4)             # zero delay -> 10 fps
        for i, (frame, _) in enumerate(out):
            self.assertEqual(frame[1, 62].tolist(), [0, 0, 0])
            self.assertEqual(frame[15, i * 15 + 5].tolist(), [0, 0, 255])
        # the square moves: frame 2 must not still show the square of frame 0
        self.assertEqual(out[2][0][15, 5].tolist(), [0, 0, 0])

    def test_gif_delay_is_kept(self):
        frames = [Image.new("RGB", (64, 32), (i * 80, 0, 0)) for i in range(3)]
        raw = encode(frames[0], "GIF", save_all=True, append_images=frames[1:],
                     duration=[40, 250, 1000])
        self.assertEqual([d for _, d in ic.convert_upload(raw, W, H)[0]], [0.04, 0.25, 1.0])

    def test_sixteen_bit_png_keeps_its_gradient(self):
        arr = np.linspace(0, 65535, 100 * 50).reshape(50, 100).astype(np.uint16)
        frame = ic.convert_upload(encode(Image.fromarray(arr), "PNG"), W, H)[0][0][0]
        self.assertTrue(100 < frame.mean() < 155)
        self.assertLess(int(frame[0, 0, 0]), 40)
        self.assertGreater(int(frame[-1, -1, 0]), 215)

    def test_exif_orientation_is_applied(self):
        img = Image.new("RGB", (50, 100), (255, 255, 0))
        exif = Image.Exif()
        exif[0x0112] = 6                                              # stored on its side
        frame = ic.convert_upload(encode(img, "JPEG", exif=exif), W, H)[0][0][0]
        # upright it is 100x50 and fills the whole panel
        self.assertGreater(int((frame.sum(axis=2) > 100).sum()), W * H * 0.9)

    def test_animated_webp(self):
        frames = [Image.new("RGB", (64, 32), (0, i * 100, 0)) for i in range(3)]
        raw = encode(frames[0], "WEBP", save_all=True, append_images=frames[1:], duration=80,
                     lossless=True)
        out, info = ic.convert_upload(raw, W, H)
        self.assertEqual((info["format"], len(out)), ("WEBP", 3))

    def test_long_animation_is_cut(self):
        frames = [Image.new("L", (8, 4), i % 255) for i in range(ic.MAX_FRAMES + 20)]
        raw = encode(frames[0], "GIF", save_all=True, append_images=frames[1:], duration=50)
        out, info = ic.convert_upload(raw, W, H)
        self.assertEqual(len(out), ic.MAX_FRAMES)
        self.assertTrue(info["truncated"])

    def test_errors_are_readable(self):
        cases = ((b"", "empty"), (b"hello world, not an image", "not a picture"),
                 (b"\x00\x00\x00\x18ftypheic" + b"\x00" * 32, "HEIC"))
        for raw, word in cases:
            with self.assertRaises(ic.ConvertError) as ctx:
                ic.convert_upload(raw, W, H)
            self.assertIn(word, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
