"""The faces must show the artist's PNGs exactly; needs only NumPy and Pillow.

Run from the project root: python -B -m unittest discover -s tests -v
"""
import importlib
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
ART = ROOT / "assets" / "emotion_masks_64x32"
FILES = {"angry": "angry", "bad": "bad", "happy": "happy", "sad": "sad",
         "scared": "испуг", "surprised": "удивление"}
TOL = 1.0 / 255.0 + 1e-6


def png(name, raw=False):
    """A part of the artwork as coverage 0..1. The firmware drops the faint haze
    around the shapes (below 6 %), so the expected pictures do the same."""
    cov = np.asarray(Image.open(ART / f"{name}.png").convert("L"), dtype=np.float32) / 255.0
    if not raw:
        cov = cov.copy()
        cov[cov < 0.06] = 0.0
        cov[cov > 0.94] = 1.0
    return cov


def load_emotions(art):
    """Import pi/emotions.py with a stand-in config (no /tmp mkdir, no hardware)."""
    sys.path.insert(0, str(ROOT / "pi"))
    config = SimpleNamespace(W=64, H=32, BANDS=16, EMOTION_ART=art)
    with patch.dict(sys.modules, {"config": config}):
        sys.modules.pop("emotions", None)
        module = importlib.import_module("emotions")
    sys.modules.pop("emotions", None)
    return module


class ArtworkFaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.emotions = load_emotions(art=True)
        sys.path.insert(0, str(ROOT / "pi"))
        cls.art = importlib.import_module("emotion_art")
        cls.nose = png("nose")

    def expected(self, eid, eye, mouth):
        """The resting face as the artist drew it: eye + mouth + nose."""
        pre = FILES[eid]
        eye_mask = png(f"{pre}_{eye}_eye")
        if eid == "angry":
            # angry_open_eye.png also contains a copy of the mouth (export slip):
            # everything right of the eye in that file is not the eye
            eye_mask = eye_mask.copy()
            eye_mask[:, 28:] = 0.0
        return np.maximum.reduce([eye_mask, png(f"{pre}_{mouth}_mouth"), self.nose])

    def test_artwork_is_loaded_for_all_six(self):
        self.assertEqual(set(self.emotions.ART_FACES), set(self.emotions.EMOTIONS))

    def test_distance_field_round_trip_is_exact(self):
        for path in sorted(ART.glob("*.png")):
            cov = self.art.clean(np.asarray(Image.open(path).convert("L"), dtype=np.float32) / 255.0)
            back = self.art.from_sdf(self.art.to_sdf(cov))
            self.assertLessEqual(float(np.abs(back - cov).max()), 1e-5, path.name)

    def test_resting_face_is_the_artwork(self):
        for eid in FILES:
            for level, mouth in ((0.0, "close"), (1.0, "open")):
                got = self.emotions.get_face_alpha(eid, level)
                self.assertEqual(got.shape, (32, 64))
                self.assertLessEqual(float(np.abs(got - self.expected(eid, "open", mouth)).max()),
                                     TOL, f"{eid} mouth {mouth}")

    def test_stray_mouth_is_removed_from_the_angry_eye(self):
        raw = png("angry_open_eye")
        self.assertGreater(float(raw[:, 30:].sum()), 200.0)          # the slip is in the file
        self.assertLess(float(np.abs(raw - png("angry_open_eye", raw=True)).max()), 0.061)
        eye = self.emotions.ART_FACES["angry"].cov["eye_open"]
        self.assertEqual(float(eye[:, 30:].sum()), 0.0)
        np.testing.assert_array_equal(eye[:, :28], raw[:, :28])      # the eye itself is intact

    def test_blink_closes_the_eye_and_leaves_the_rest(self):
        e = self.emotions
        for eid in FILES:
            opened = e.get_face_alpha_animated(eid, eid, 0.0, 0.0)
            half = e.get_face_alpha_animated(eid, eid, 0.0, 0.0, blink_level=0.5)
            shut = e.get_face_alpha_animated(eid, eid, 0.0, 0.0, blink_level=1.0)
            eye = e.ART_FACES[eid].cov["eye_open"] > 0.5
            ys = np.nonzero(eye.any(axis=1))[0]
            height = lambda f: int((f[:, :28] > 0.5).any(axis=1)[ys[0]:ys[-1] + 1].sum())
            self.assertLessEqual(float(shut[:, :28].sum()), 1.3 * float(opened[:, :28].sum()), eid)
            self.assertLessEqual(height(half), height(opened), eid)
            self.assertGreater(float(shut[:, :28].sum()), 3.0, f"{eid}: closed eye vanished")
            np.testing.assert_allclose(shut[:, 30:], opened[:, 30:], atol=1e-6)   # mouth, nose

    def test_transition_ends_on_the_artwork(self):
        e = self.emotions
        for a, b in (("angry", "happy"), ("scared", "sad"), ("surprised", "love")):
            start = e.get_face_alpha_animated(a, b, 0.0, 0.3)
            end = e.get_face_alpha_animated(a, b, 1.0, 0.3)
            np.testing.assert_allclose(start, e.get_face_alpha(a, 0.3), atol=1e-6)
            np.testing.assert_allclose(end, e.get_face_alpha(b, 0.3), atol=1e-6)

    def test_every_in_between_frame_is_a_valid_picture(self):
        e = self.emotions
        ids = list(e.EMOTIONS)
        for i, a in enumerate(ids):
            b = ids[(i + 3) % len(ids)]
            for blend in (0.1, 0.35, 0.5, 0.8):
                frame = e.get_face_alpha_animated(a, b, blend, 0.5, 0.4, 1.5, -0.7, 0.6)
                self.assertTrue(np.isfinite(frame).all())
                self.assertGreaterEqual(float(frame.min()), 0.0)
                self.assertLessEqual(float(frame.max()), 1.0)
                self.assertGreater(float(frame.sum()), 20.0, f"{a}->{b} at {blend} is empty")

    def test_mouth_opens_gradually(self):
        for eid in FILES:
            lit = [float(self.emotions.get_face_alpha(eid, t).sum()) for t in (0, 0.25, 0.5, 0.75, 1)]
            closed, opened = lit[0], lit[-1]
            lo, hi = min(closed, opened), max(closed, opened)
            for value in lit[1:-1]:
                self.assertTrue(lo * 0.6 <= value <= hi * 1.15, f"{eid}: {lit}")

    def test_shift_moves_whole_pixels_exactly(self):
        field = self.art.to_sdf(png("angry_open_eye"))
        moved = self.art.shift(field, 3, -2)
        np.testing.assert_array_equal(moved[:-2, 3:], field[2:, :-3])
        half = self.art.from_sdf(self.art.shift(field, 0.5, 0.0))
        whole = self.art.from_sdf(field)
        self.assertAlmostEqual(float(half.sum()), float(whole.sum()), delta=whole.sum() * 0.05)

    def test_static_masks_for_the_renderer(self):
        masks = self.emotions.MASKS["scared"]
        self.assertEqual(masks.eye.dtype, np.uint8)
        np.testing.assert_array_equal(
            masks.mouth_open, (png("испуг_open_mouth") * 255 + 0.5).astype(np.uint8))

    def test_emotions_without_artwork_still_have_a_face(self):
        for eid in ("sleepy", "love", "evil"):
            frame = self.emotions.get_face_alpha(eid, 0.0)
            self.assertGreater(float(frame.sum()), 30.0, eid)


class LayoutAndMotionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.emotions = load_emotions(art=True)
        sys.path.insert(0, str(ROOT / "pi"))
        cls.live = importlib.import_module("liveliness")

    def test_every_face_has_the_eye_on_the_left_and_the_mouth_on_the_right(self):
        for eid, face in self.emotions.ART_FACES.items():
            eye_x = np.nonzero(face.cov["eye_open"] > 0.5)[1].mean()
            mouth_x = np.nonzero(face.cov["mouth_open"] > 0.5)[1].mean()
            self.assertLess(eye_x, 28, eid)
            self.assertGreater(mouth_x, 30, eid)

    def test_highlight_sits_on_the_eye(self):
        for eid, face in self.emotions.ART_FACES.items():
            x, y = (int(v) for v in self.emotions.get_layout(eid).catchlight)
            self.assertGreater(float(face.cov["eye_open"][y, x]), 0.8, eid)
            self.assertGreater(float(face.cov["eye_open"][y, x + 1]), 0.8, eid)

    def test_motion_is_small_smooth_and_can_be_switched_off(self):
        for eid in self.emotions.EMOTIONS:
            self.assertIn(eid, self.live.CHARACTERS)
            prev = None
            for n in range(900):                                  # 30 s at 30 fps
                m = self.live.motion(eid, eid, 1.0, n / 30.0)
                self.assertLessEqual(abs(m.eye_dx), 1.5, eid)
                self.assertLessEqual(abs(m.eye_dy), 1.5, eid)
                self.assertLessEqual(abs(m.eye_grow), 1.2, eid)
                self.assertTrue(0.0 <= m.eye_squint <= 0.8, eid)
                if prev is not None and eid != "sleepy":         # sleepy jerks awake on purpose
                    self.assertLess(abs(m.eye_dy - prev.eye_dy), 0.6, eid)
                    self.assertLess(abs(m.eye_grow - prev.eye_grow), 0.6, eid)
                prev = m
        off = self.live.motion("love", "love", 1.0, 3.3, mouth=1.0, beat=1.0, amount=0.0)
        self.assertEqual([getattr(off, k) for k in off.__slots__], [0.0] * 6)

    def test_grown_and_shrunk_faces_are_valid_and_keep_thin_lines(self):
        e = self.emotions
        for eid in e.EMOTIONS:
            rest = e.get_face_alpha_animated(eid, eid, 1.0, 0.0)
            big = e.get_face_alpha_animated(eid, eid, 1.0, 0.0, eye_grow=1.0, mouth_grow=0.5)
            small = e.get_face_alpha_animated(eid, eid, 1.0, 0.0, eye_grow=-2.0, mouth_grow=-2.0)
            self.assertGreater(float(big.sum()), float(rest.sum()), eid)
            self.assertGreater(float(small[:, :28].sum()), 3.0, f"{eid}: eye vanished")
            self.assertGreater(float(small[:, 30:].sum()), 3.0, f"{eid}: mouth vanished")


class TransitionAndMouthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.emotions = load_emotions(art=True)

    def test_faces_without_artwork_borrow_a_drawn_mouth_and_nose(self):
        faces = self.emotions.ART_FACES
        for eid, donor in self.emotions.MOUTH_FROM.items():
            for part in ("mouth_closed", "mouth_open"):
                np.testing.assert_array_equal(faces[eid].cov[part], faces[donor].cov[part])
            self.assertTrue((faces[eid].cov["nose"] >= faces[donor].cov["nose"]).all(), eid)

    def test_transition_goes_in_steps(self):
        stages = lambda t: tuple(self.emotions.transition_stages(t))[:4]
        self.assertEqual(stages(0.0), (0.0, 0.0, 0.0, 0.0))
        self.assertEqual(stages(1.0), (1.0, 1.0, 0.0, 0.0))
        eye, mouth, wink, dip = stages(0.3)
        self.assertGreater(eye, 0.3)                 # the eye is on its way
        self.assertEqual(mouth, 0.0)                 # the mouth has not started
        self.assertGreater(wink, 0.4)                # and the eye is squeezed
        eye, mouth, wink, dip = stages(0.5)
        self.assertGreater(eye, 0.9)                 # the eye is almost there
        self.assertLess(mouth, 0.8)                  # the mouth is behind
        self.assertAlmostEqual(stages(0.8)[2], 0.0)      # the squeeze is over before the end

    def test_eye_halo_covers_the_eye_and_not_the_mouth(self):
        e = self.emotions
        for eid in e.EMOTIONS:
            halo = e.get_eye_halo(eid, eid, 1.0, eye_dx=1.5)
            eye = e.ART_FACES[eid].cov["eye_open"]
            shifted = np.zeros_like(eye)
            shifted[:, 2:] = eye[:, :-2]
            self.assertTrue((halo[shifted > 0.5] > 0.99).all(), eid)
            self.assertLess(float(halo[:, 40:].max()), 0.01, eid)


class DrawnExtrasTests(unittest.TestCase):
    """Eyes for sleepy/love/evil, drawn closed eyes, transitions, mouth shapes."""

    @classmethod
    def setUpClass(cls):
        cls.emotions = load_emotions(art=True)

    def test_all_nine_come_from_pictures_with_a_drawn_closed_eye(self):
        for eid, face in self.emotions.ART_FACES.items():
            self.assertIsNotNone(face.blink_sdf, f"{eid}: no lid line")
        for eid, pre in (("sleepy", "sleepy"), ("love", "love"), ("evil", "evil")):
            np.testing.assert_allclose(self.emotions.ART_FACES[eid].cov["eye_open"],
                                       png(f"{pre}_open_eye"), atol=1e-6)

    def test_full_blink_is_the_drawn_lid_line(self):
        e = self.emotions
        names = dict(FILES, sleepy="sleepy", love="love", evil="evil")
        for eid, pre in names.items():
            shut = e.get_face_alpha_animated(eid, eid, 1.0, 0.0, blink_level=1.0)
            line = png(f"{pre}_blink_eye")
            self.assertLessEqual(float(np.abs(shut[:, :26] - line[:, :26]).max()), 0.02, eid)

    def test_blink_goes_down_steadily(self):
        e = self.emotions
        for eid in e.EMOTIONS:
            lit = [float(e.get_face_alpha_animated(eid, eid, 1.0, 0.0, blink_level=b)[:, :28].sum())
                   for b in (0.0, 0.2, 0.4, 0.6, 0.7)]
            for before, after in zip(lit, lit[1:]):
                self.assertLessEqual(after, before * 1.05 + 2.0, f"{eid}: {lit}")
            self.assertGreater(min(lit), 8.0, f"{eid}: the eye vanishes mid-blink {lit}")

    def test_every_emotion_arrives_in_its_own_way_and_lands_exactly(self):
        e = self.emotions
        seen = set()
        for eid in e.EMOTIONS:
            self.assertEqual(tuple(e.transition_stages(0.0, eid)), (0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0))
            self.assertEqual(tuple(e.transition_stages(1.0, eid)), (1.0, 1.0, 0.0, 0.0, 1.0, 0.0, 0.0))
            seen.add(tuple(round(v, 3) for t in (0.2, 0.4, 0.6) for v in e.transition_stages(t, eid)))
            late = e.transition_stages(0.98, eid)             # no jump on the last frame
            self.assertGreater(late.eye, 0.97, eid)
            self.assertLess(abs(late.grow) + abs(late.dx) + late.wink + abs(late.dip), 0.25, eid)
            self.assertGreater(late.scale, 0.97, eid)
            for t in np.linspace(0.05, 0.95, 19):
                frame = e.get_face_alpha_animated("angry" if eid != "angry" else "sad", eid, float(t), 0.3)
                self.assertTrue(np.isfinite(frame).all())
                self.assertGreater(float(frame[:, 30:].sum()), 5.0, f"{eid} at {t}: mouth vanished")
                self.assertGreater(float(frame[:, :26].sum()), 2.5, f"{eid} at {t}: eye vanished")
        self.assertGreaterEqual(len(seen), 8)

    def test_mouth_shapes(self):
        e = self.emotions
        for eid in e.EMOTIONS:
            plain = e.get_face_alpha_animated(eid, eid, 1.0, 1.0)
            round_ = e.get_face_alpha_animated(eid, eid, 1.0, 1.0, mouth_round=1.0)
            wide = e.get_face_alpha_animated(eid, eid, 1.0, 1.0, mouth_wide=1.0)
            face = e.ART_FACES[eid]
            outer = slice(int(face.mouth_x0), int(face.mouth_x0 + (face.mouth_x1 - face.mouth_x0) * 0.3))
            self.assertLess(float(round_[14:, outer].sum()), 0.5 * float(plain[14:, outer].sum()) + 1.0, eid)
            self.assertLess(float(wide[:, 30:].sum()), float(plain[:, 30:].sum()), eid)
            self.assertGreater(float(wide[:, 30:].sum()), 10.0, eid)
            np.testing.assert_allclose(round_[:, :25], plain[:, :25], atol=1e-6)     # the eye is not touched
            closed = e.get_face_alpha_animated(eid, eid, 1.0, 0.0, mouth_round=1.0, mouth_wide=1.0)
            np.testing.assert_allclose(closed, e.get_face_alpha(eid, 0.0), atol=1e-6)


class EyeStyleTests(unittest.TestCase):
    def test_two_styles_same_mouth_and_switching(self):
        e = load_emotions(art=True)
        names = dict(FILES, sleepy="sleepy", love="love", evil="evil")
        self.assertEqual(e.EYE_STYLE, "stylised")
        plain = {eid: e.get_face_alpha(eid, 0.4) for eid in names}
        self.assertTrue(e.set_eye_style(1))
        self.assertFalse(e.set_eye_style("anatomical"))             # already set
        for eid, pre in names.items():
            frame = e.get_face_alpha(eid, 0.4)
            eye = png(f"{pre}_real_eye")
            # put together from the eyeball and the pupil: the same picture but for the pupil's rim
            self.assertLess(float(np.abs(frame[:, :25] - eye[:, :25]).mean()), 0.01, eid)
            self.assertTrue(e.has_pupil(eid), eid)
            np.testing.assert_allclose(frame[:, 32:], plain[eid][:, 32:], atol=1e-6)   # mouth, nose
            x, y = (int(v) for v in e.get_layout(eid).catchlight)
            self.assertGreater(float(eye[y, x]), 0.8, f"{eid}: highlight off the eye")
            shut = e.get_face_alpha_animated(eid, eid, 1.0, 0.0, blink_level=1.0)
            self.assertLess(float(np.abs(shut[:, :26] - png(f"{pre}_blink_eye")[:, :26]).max()), 0.02, eid)
        # the pupil moves inside the eye; the eyeball stays where it is
        for eid in names:
            ball = e.ART_FACES and e.STYLE_FACES["anatomical"][eid].cov["eye_open"]
            xs = np.arange(64, dtype=np.float32)[None, :]
            where = []
            for dx in (-2.5, 0.0, 2.5):
                frame = e.get_face_alpha_animated(eid, eid, 1.0, 0.0, pupil_dx=dx)
                dark = np.clip(ball - frame, 0.0, 1.0)[:, :27]
                self.assertGreater(float(dark.sum()), 1.5, f"{eid}: pupil gone at {dx}")
                where.append(float((dark * xs[:, :27]).sum() / dark.sum()))
                self.assertLessEqual(float((frame - ball)[:, :25].max()), 0.02, f"{eid}: eyeball changed")
            self.assertLess(where[0] + 1.0, where[1], eid)
            self.assertLess(where[1] + 1.0, where[2], eid)
            old, new = e.get_pupil_holes(eid, eid, 1.0, gazes=((2.0, 0.0), (-2.0, 0.0)))
            self.assertGreater(float(old.sum()), 1.5, eid)
            self.assertLess(float((new * xs).sum() / new.sum()) + 2.0, float((old * xs).sum() / old.sum()), eid)
            shut = e.get_pupil_holes(eid, eid, 1.0, blink_level=1.0)[0]
            self.assertLess(float(shut.sum()), 1.0, f"{eid}: pupil shows through the shut eye")
        self.assertTrue(e.set_eye_style(0))
        self.assertFalse(e.has_pupil("angry"))
        for eid in names:
            np.testing.assert_array_equal(e.get_face_alpha(eid, 0.4), plain[eid])
        self.assertFalse(e.set_eye_style("no such style"))


class CustomEmotionTests(unittest.TestCase):
    def test_add_use_reload_and_remove(self):
        import tempfile
        e = load_emotions(art=True)
        eye = png("love_open_eye")
        mouth = png("sad_close_mouth")
        with tempfile.TemporaryDirectory() as tmp:
            e.CUSTOM_DIR = os.path.join(tmp, "emotions")
            before = set(e.EMOTIONS)
            eid = e.add_custom("Моя", "🙂", {"eye_open": eye, "mouth_closed": mouth})
            self.assertNotIn(eid, before)
            self.assertIn({"id": eid, "emoji": "🙂", "label": "Моя", "custom": True}, e.list_emotions())
            frame = e.get_face_alpha_animated("angry", eid, 1.0, 0.0)
            self.assertLess(float(np.abs(frame[:, :26] - eye[:, :26]).max()), 0.02)
            opened = e.get_face_alpha(eid, 1.0)                  # stand-in open mouth: the line made fat
            self.assertGreater(float(opened[:, 30:].sum()), float(frame[:, 30:].sum()) * 1.5)
            self.assertIsNotNone(e.get_layout(eid))
            for part in ("eye_open", "mouth_closed"):
                with self.assertRaises(ValueError):
                    e.add_custom("x", "", {k: v for k, v in (("eye_open", eye), ("mouth_closed", mouth))
                                           if k != part})
            # after a restart it is still there
            for store in (e.CUSTOM, e.EMOTIONS, e.ART_FACES, e.MASKS, e.LAYOUTS):
                store.pop(eid)
            e.load_custom()
            self.assertIn(eid, e.ART_FACES)
            self.assertTrue(e.remove_custom(eid))
            self.assertFalse(e.remove_custom(eid))
            self.assertFalse(e.remove_custom("angry"))
            self.assertEqual(set(e.EMOTIONS), before)
            self.assertEqual(os.listdir(e.CUSTOM_DIR), [])


class PolygonFallbackTests(unittest.TestCase):
    def test_switch_off_gives_the_polygon_faces(self):
        emotions = load_emotions(art=False)
        self.assertEqual(emotions.ART_FACES, {})
        frame = emotions.get_face_alpha("angry", 0.0)
        self.assertGreater(float(np.abs(frame - png("angry_close_mouth")).max()), 0.5)

    def test_missing_artwork_falls_back(self):
        sys.path.insert(0, str(ROOT / "pi"))
        config = SimpleNamespace(W=64, H=32, BANDS=16, EMOTION_ART=True)
        with patch.dict(sys.modules, {"config": config}):
            sys.modules.pop("emotions", None)
            with patch("os.path.exists", return_value=False):
                emotions = importlib.import_module("emotions")
        sys.modules.pop("emotions", None)
        self.assertEqual(emotions.ART_FACES, {})
        self.assertGreater(float(emotions.get_face_alpha("happy", 1.0).sum()), 30.0)


if __name__ == "__main__":
    unittest.main()
