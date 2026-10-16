"""Rendering regression checks; no Pi hardware or running services required.

Run from the project root: python -B -m unittest discover -s tests -v
"""
import colorsys
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw


def load_render_modules():
    pi_dir = Path(__file__).resolve().parents[1] / "pi"
    # Avoid GPIO initialization, persistence threads and config's mkdir.
    substitutes = {
        "config": SimpleNamespace(W=64, H=32, BANDS=16),
        "rgbmatrix": SimpleNamespace(RGBMatrix=None, RGBMatrixOptions=None),
        "state": SimpleNamespace(**dict.fromkeys((
            "state", "lock", "stop_event", "log_err", "target_bars",
            "uploaded_image", "uploaded_gif_frames",
        ))),
    }
    modules = {}
    with patch.dict(sys.modules, substitutes):
        for name in ("emotions", "boot_animation", "animations", "postfx",
                     "dino_game", "matrix_renderer"):
            spec = importlib.util.spec_from_file_location(name, pi_dir / f"{name}.py")
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            modules[name] = module
    return modules["matrix_renderer"], modules["emotions"]


renderer, emotions = load_render_modules()


def reference_mask(polygons):
    """Draw separate masks and union them, as the original renderer did."""
    result = np.zeros((32, 64), dtype=np.uint8)
    for points in polygons:
        image = Image.new("L", (64, 32))
        ImageDraw.Draw(image).polygon([tuple(p) for p in points], fill=255)
        result = np.maximum(result, np.asarray(image))
    return result


class GradientTests(unittest.TestCase):
    def setUp(self):
        renderer._cached_gradient_map.cache_clear()

    def test_repeated_gradient_reuses_read_only_map(self):
        colors = [[255, 0, 0], [0, 0, 255]]
        first = renderer._make_gradient_map(colors, 0)
        self.assertIs(first, renderer._make_gradient_map([c[:] for c in colors], 0))
        self.assertEqual(first.dtype, np.float32)
        self.assertFalse(first.flags.writeable)

    def test_color_and_angle_edits_invalidate_cache(self):
        colors = [[255, 0, 0], [0, 0, 255]]
        first = renderer._make_gradient_map(colors, 0)
        colors[0][0] = 10
        changed = renderer._make_gradient_map(colors, 0)
        np.testing.assert_array_equal(first[0, 0], [255, 0, 0])
        np.testing.assert_array_equal(changed[0, 0], [10, 0, 0])
        rotated = renderer._make_gradient_map(colors, 180)
        np.testing.assert_allclose(rotated[0, -1], [10, 0, 0], atol=1e-4)
        self.assertEqual(renderer._cached_gradient_map.cache_info().currsize, 1)

    def test_dimensions_are_part_of_cache_key(self):
        colors = [[10, 20, 30], [40, 50, 60]]
        original = renderer._make_gradient_map(colors, 45)
        with patch.object(renderer.config, "W", 128):
            wide = renderer._make_gradient_map(colors, 45)
        self.assertEqual(original.shape, (32, 64, 3))
        self.assertEqual(wide.shape, (32, 128, 3))

    def test_single_color_fills_panel(self):
        actual = renderer._make_gradient_map([[12, 34, 56]], 123)
        expected = np.full((32, 64, 3), [12, 34, 56], dtype=np.float32)
        np.testing.assert_array_equal(actual, expected)
        self.assertFalse(actual.flags.writeable)

    def test_multiple_stops_preserve_linear_interpolation(self):
        colors = [[255, 0, 0], [0, 255, 0], [0, 0, 255]]
        actual = renderer._make_gradient_map(colors, 0)
        positions = np.linspace(0, 2, 64)
        row = np.stack([
            np.interp(positions, [0, 1, 2], np.asarray(colors)[:, channel])
            for channel in range(3)
        ], axis=-1)
        np.testing.assert_allclose(actual, np.broadcast_to(row, actual.shape), atol=1e-4)


class RainbowTests(unittest.TestCase):
    def test_all_hues_match_pillow(self):
        for hue in range(256):
            expected = Image.new("HSV", (1, 1), (hue, 255, 255)).convert("RGB").getpixel((0, 0))
            np.testing.assert_array_equal(renderer._rainbow_palette()[hue], expected)

    def test_diagonal_wrap_and_fractional_offsets(self):
        diag = np.indices((32, 64)).sum(axis=0).astype(np.int32)
        for offset in (-256.5, 0, 0.1, 37.25, 255, 256, 9999.5):
            hue = ((diag * 16 - offset) % 256).astype(np.uint8)
            hsv = np.stack([hue, np.full_like(hue, 255), np.full_like(hue, 255)], axis=-1)
            expected = Image.frombytes("HSV", (64, 32), hsv.tobytes()).convert("RGB")
            np.testing.assert_array_equal(renderer._make_rainbow_map(diag, offset), expected)

    def test_text_colors_preserve_glyphs_and_scrolling(self):
        for width in (64, 128):
            for scale in (1, 2, 3):
                for offset in (0, 5, 63, 90, 150, 999):
                    for color in ((200, 0, 80), (0, 0, 0)):
                        args = ("Hi ? 123", offset, color, width, scale)
                        expected = renderer._render_text(*args, effect="solid")
                        for y, x in zip(*np.nonzero(expected.any(axis=2))):
                            hue = (x / width + 1.234 * 0.3) % 1.0
                            expected[y, x] = [int(c * 255) for c in colorsys.hsv_to_rgb(hue, 1, 1)]
                        actual = renderer._render_text(*args, effect="rainbow", t=1.234)
                        np.testing.assert_array_equal(actual, expected)

    def test_empty_text_stays_black(self):
        for text in ("", "    "):
            self.assertFalse(renderer._render_text(text, 128, (255, 0, 0), 128,
                                                   effect="rainbow").any())


class FaceTests(unittest.TestCase):
    def test_polygon_union_matches_separate_rasterizations(self):
        rng = np.random.default_rng(73)
        for _ in range(30):
            # Include overlap, fractional coordinates and off-screen vertices.
            polygons = [rng.uniform((-10, -10), (74, 42), size=(20, 2)).astype(np.float32)
                        for _ in range(3)]
            np.testing.assert_array_equal(emotions._rasterize_polys(polygons), reference_mask(polygons))
            np.testing.assert_array_equal(emotions._rasterize_poly(polygons[0]), reference_mask(polygons[:1]))

    def test_static_faces_match_polygon_geometry(self):
        for emotion in emotions.EMOTIONS:
            for mouth in (0, 0.4, 1):
                closed, opened = emotions._MOUTH_SLOTS[emotion]
                polygons = [slot[emotion] for slot in emotions._EYE_SLOTS]
                polygons.append(closed * (1 - mouth) + opened * mouth)
                expected = reference_mask(polygons).astype(np.float32) / 255.0
                np.testing.assert_array_equal(emotions.get_face_alpha(emotion, mouth), expected)

    def test_transition_endpoints_and_unknown_emotion(self):
        for emotion in (*emotions.EMOTIONS, "unknown"):
            for mouth in (0, 0.5, 1):
                expected = emotions.get_face_alpha(emotion, mouth)
                for start, end, blend in ((emotion, "angry", 0), ("angry", emotion, 1)):
                    np.testing.assert_array_equal(
                        emotions.get_face_alpha_morph(start, end, blend, mouth), expected)
                    np.testing.assert_array_equal(
                        emotions.get_face_alpha_animated(start, end, blend, mouth), expected)

    def test_animation_does_not_mutate_shared_geometry(self):
        arrays = [points for slot in emotions._EYE_SLOTS for points in slot.values()]
        arrays += [points for pair in emotions._MOUTH_SLOTS.values() for points in pair]
        originals = [points.copy() for points in arrays]
        for blend in (0, 0.15, 0.5, 1):
            emotions.get_face_alpha_animated("angry", "happy", blend, 0.6, 0.8, 2, -1, 3)
        for original, points in zip(originals, arrays):
            np.testing.assert_array_equal(points, original)


class FlashTests(unittest.TestCase):
    """A flash must not light the panel up to its border (the rectangle would show)."""

    def setUp(self):
        self.falloff = renderer.flash_falloff(32, 64)

    def test_falloff_is_full_in_the_middle_and_nothing_at_the_border(self):
        f = self.falloff
        self.assertGreater(float(f[15:17, 31:33].min()), 0.9)
        border = np.concatenate([f[0], f[-1], f[:, 0], f[:, -1]])
        self.assertLess(float(border.max()), 0.02)
        self.assertTrue(np.all(np.diff(f[16, 32:]) <= 1e-6))       # only ever darker towards the edge

    def test_empty_background_stays_dark_at_the_border(self):
        out = renderer.add_flash(np.zeros((32, 64, 3), np.uint8), 1.0, self.falloff)
        self.assertGreater(int(out[16, 32, 0]), 150)
        for edge in (out[0], out[-1], out[:, 0], out[:, -1]):
            self.assertLess(int(edge.max()), 5)

    def test_the_face_gets_the_whole_flash_wherever_it_is(self):
        frame = np.zeros((32, 64, 3), np.uint8)
        frame[1, 1] = (100, 0, 0)                                   # a lit pixel in the corner
        out = renderer.add_flash(frame, 0.5, self.falloff)
        self.assertEqual(tuple(out[1, 1]), (200, 100, 100))
        self.assertEqual(int(out[1, 2].max()), 0)                   # its dark neighbour: nothing

    def test_no_overflow(self):
        frame = np.full((32, 64, 3), 250, np.uint8)
        self.assertEqual(int(renderer.add_flash(frame, 1.0, self.falloff).min()), 255)


if __name__ == "__main__":
    unittest.main()
