"""Loudness calibration, emotions from sound, gestures, winks: pure logic, NumPy only.

Run from the project root: python -B -m unittest discover -s tests -v
"""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pi"))
import reactions                                                    # noqa: E402

DT = 1.0 / 30.0


def feat(rms=0.0, beat=0.0, silence=0.0, rms_slow=0.0):
    return SimpleNamespace(rms=rms, beat=beat, silence_time=silence, rms_slow=rms_slow)


class AutoGainTests(unittest.TestCase):
    def speech(self, n, level, noise):
        t = np.arange(16)
        out = np.full(16, noise, dtype=np.float32)
        out[3:9] += level * (0.5 + 0.5 * np.sin(n * 0.8)) * np.exp(-((t[3:9] - 5.5) / 2.0) ** 2)
        return out

    def run_room(self, level, noise, seconds=20):
        agc = reactions.AutoGain(16)
        peaks = []
        for n in range(int(seconds * 30)):
            out = agc.process(self.speech(n, level, noise), DT)
            if n > seconds * 15:
                peaks.append(float(out.max()))
        return agc, np.array(peaks)

    def test_quiet_and_loud_rooms_give_the_same_picture(self):
        _, quiet = self.run_room(level=0.12, noise=0.0)
        _, loud = self.run_room(level=0.9, noise=0.0)
        self.assertGreater(np.percentile(quiet, 90), 0.8)
        self.assertGreater(np.percentile(loud, 90), 0.8)
        self.assertLess(abs(np.percentile(quiet, 90) - np.percentile(loud, 90)), 0.2)

    def test_steady_background_is_removed(self):
        agc, _ = self.run_room(level=0.3, noise=0.35, seconds=60)
        out = agc.process(np.full(16, 0.35, dtype=np.float32), DT)      # only the hum is left
        self.assertLess(float(out.max()), 0.1)

    def test_noise_is_not_blown_up_in_silence(self):
        agc = reactions.AutoGain(16)
        rng = np.random.default_rng(1)
        top = 0.0
        for n in range(30 * 60):                                       # a minute of faint hiss
            out = agc.process(rng.uniform(0.0, 0.02, 16).astype(np.float32), DT)
            if n > 300:
                top = max(top, float(out.max()))
        self.assertLess(top, 0.35)

    def test_follows_a_change_within_seconds(self):
        agc = reactions.AutoGain(16)
        for n in range(30 * 10):
            agc.process(self.speech(n, 0.9, 0.0), DT)
        last = []
        for n in range(30 * 40):                                       # everybody starts to whisper
            out = agc.process(self.speech(n, 0.1, 0.0), DT)
            if n > 30 * 30:
                last.append(float(out.max()))
        self.assertGreater(np.percentile(last, 90), 0.7)
        out = agc.process(self.speech(2, 0.9, 0.0), DT)                 # and a shout: clipped, not broken
        self.assertLessEqual(float(out.max()), 1.0)


class AutoEmotionTests(unittest.TestCase):
    def test_bang_after_quiet_scares_then_passes(self):
        auto = reactions.AutoEmotion()
        t = 0.0
        for _ in range(120):
            t += DT
            self.assertIsNone(auto.update(feat(), t, "angry", raw_rms=0.0))
        self.assertEqual(auto.update(feat(rms=0.8), t + DT, "angry", raw_rms=0.6), "scared")
        self.assertEqual(auto.update(feat(rms=0.1), t + 1.0, "angry", raw_rms=0.05), "scared")
        self.assertIsNone(auto.update(feat(rms=0.1), t + 3.0, "angry", raw_rms=0.05))

    def test_normal_speech_after_quiet_does_not_scare(self):
        auto = reactions.AutoEmotion()
        t = 0.0
        for _ in range(120):
            t += DT
            auto.update(feat(), t, "angry", raw_rms=0.0)
        self.assertIsNone(auto.update(feat(rms=0.6), t + DT, "angry", raw_rms=0.08))

    def test_music_makes_happy_and_silence_sleepy(self):
        auto = reactions.AutoEmotion()
        t, got = 0.0, None
        for n in range(30 * 6):
            t += DT
            got = auto.update(feat(rms=0.4, beat=1.0 if n % 15 == 0 else 0.0, rms_slow=0.3), t, "angry",
                              raw_rms=0.3)
        self.assertEqual(got, "happy")
        for _ in range(30 * 10):
            t += DT
            got = auto.update(feat(silence=t), t, "angry", raw_rms=0.0)
        self.assertIsNone(got)
        self.assertEqual(auto.update(feat(silence=50.0), t + DT, "angry", raw_rms=0.0), "sleepy")
        self.assertIsNone(auto.update(feat(rms=0.3, silence=0.0), t + 2.0, "angry", raw_rms=0.1))

    def test_a_choice_by_hand_is_left_alone(self):
        auto = reactions.AutoEmotion()
        auto.update(feat(silence=100.0), 1.0, "angry", raw_rms=0.0)
        self.assertEqual(auto.update(feat(silence=100.0), 1.1, "angry", raw_rms=0.0), "sleepy")
        self.assertIsNone(auto.update(feat(silence=100.0), 1.2, "happy", raw_rms=0.0))     # user picked happy
        self.assertIsNone(auto.update(feat(silence=100.0), 5.0, "happy", raw_rms=0.0))
        self.assertEqual(auto.update(feat(silence=100.0), 10.0, "happy", raw_rms=0.0), "sleepy")


class GestureTests(unittest.TestCase):
    def play(self, g, seconds, gyro=lambda t: (0, 0, 0), near=lambda t: False, impact=lambda t: False, t0=0.0):
        events, emotion = [], None
        for n in range(int(seconds * 30)):
            t = n * DT
            emotion, event = g.update(t0 + t, gyro=gyro(t), near=near(t), impact=impact(t))
            if event:
                events.append(event)
        return events, emotion

    def test_nod_and_head_shake(self):
        swing = lambda t: 150.0 * np.sin(2 * np.pi * 3.0 * t)
        events, emotion = self.play(reactions.Gestures(), 1.0, gyro=lambda t: (swing(t), 0, 0))
        self.assertEqual((events, emotion), (["nod"], "happy"))
        events, emotion = self.play(reactions.Gestures(), 1.0, gyro=lambda t: (0, 0, swing(t)))
        self.assertEqual((events, emotion), (["shake"], "bad"))

    def test_walking_and_one_turn_are_not_gestures(self):
        events, emotion = self.play(reactions.Gestures(), 5.0,
                                    gyro=lambda t: (30.0 * np.sin(2 * np.pi * 2.0 * t), 0, 0))
        self.assertEqual((events, emotion), ([], None))
        events, emotion = self.play(reactions.Gestures(), 3.0,
                                    gyro=lambda t: (0, 0, 200.0 if 1.0 < t < 1.3 else 0.0))
        self.assertEqual((events, emotion), ([], None))

    def test_petting_poke_and_knocks(self):
        g = reactions.Gestures()
        events, emotion = self.play(g, 2.0, near=lambda t: t < 1.5)
        self.assertEqual((events, emotion), (["pet"], "love"))
        events, emotion = self.play(g, 4.0, t0=10.0)                    # love fades after the hand is gone
        self.assertIsNone(emotion)
        events, emotion = self.play(reactions.Gestures(), 1.0, near=lambda t: 0.2 < t < 0.4)
        self.assertEqual((events, emotion), (["boop"], None))
        hit = lambda t: abs(t - 0.2) < 0.01 or abs(t - 0.7) < 0.01
        events, emotion = self.play(reactions.Gestures(), 1.0, impact=hit)
        self.assertEqual((events, emotion), (["knock"], "angry"))
        events, emotion = self.play(reactions.Gestures(), 1.0, impact=lambda t: abs(t - 0.2) < 0.01)
        self.assertEqual((events, emotion), ([], None))


class AsymmetryTests(unittest.TestCase):
    def test_wink_shuts_one_eye_and_ends(self):
        a = reactions.Asymmetry()
        a.next_t = 1e9
        a.trigger(0.0, "wink", side=1)
        closed = [a.update(n * DT, "happy") for n in range(30)]
        self.assertEqual(max(c[0][0] for c in closed), 0.0)             # eye 0 stays open
        self.assertEqual(max(c[1][0] for c in closed), 1.0)             # eye 1 shuts fully
        self.assertEqual(closed[-1], reactions.Asymmetry.STILL)

    def test_happens_by_itself_only_where_it_suits(self):
        for emotion, expected in (("happy", True), ("surprised", False)):
            a = reactions.Asymmetry()
            odd = any(a.update(n * DT, emotion) != reactions.Asymmetry.STILL for n in range(30 * 120))
            self.assertEqual(odd, expected, emotion)
        a = reactions.Asymmetry()
        self.assertFalse(any(a.update(n * DT, "happy", enabled=False) != a.STILL for n in range(30 * 60)))


class VisemeTests(unittest.TestCase):
    def test_low_sounds_round_hissing_flat_silence_neither(self):
        low, high, mid = (np.zeros(16, dtype=np.float32) for _ in range(3))
        low[1:4], high[11:], mid[5:8] = 0.8, 0.8, 0.8
        for bars, want in ((low, (True, False)), (high, (False, True)), (mid, (False, False)),
                           (np.zeros(16, dtype=np.float32), (False, False))):
            v = reactions.Visemes()
            for _ in range(20):
                r, w = v.update(bars)
            self.assertEqual((r > 0.6, w > 0.6), want)


class PowerLimitTests(unittest.TestCase):
    """The limiter is a plain function of the renderer; loaded without hardware."""

    @classmethod
    def setUpClass(cls):
        import importlib
        from types import ModuleType
        from unittest.mock import patch
        stubs = {"rgbmatrix": ModuleType("rgbmatrix"), "serial_proto": ModuleType("serial_proto")}
        stubs["rgbmatrix"].RGBMatrix = stubs["rgbmatrix"].RGBMatrixOptions = object
        with patch.dict(sys.modules, stubs), patch("os.makedirs"):
            cls.limit = staticmethod(importlib.import_module("matrix_renderer")._limit_power)

    def test_white_screen_is_dimmed_to_the_limit(self):
        white = np.full((32, 128, 3), 255, dtype=np.uint8)
        out, load = self.limit(white, 100, 0.25)
        self.assertAlmostEqual(load, 1.0)
        self.assertAlmostEqual(float(out.mean()) / 255.0, 0.25, delta=0.01)
        out, load = self.limit(white, 40, 0.25)                 # already dimmed by the brightness
        self.assertAlmostEqual(float(out.mean()) / 255.0 * 0.4, 0.25, delta=0.01)

    def test_a_face_and_no_limit_pass_untouched(self):
        face = np.zeros((32, 128, 3), dtype=np.uint8)
        face[10:16, 20:40, 0] = 255                             # a red eye: about 1 % of full white
        out, load = self.limit(face, 100, 0.25)
        self.assertIs(out, face)
        self.assertLess(load, 0.02)
        white = np.full((32, 128, 3), 255, dtype=np.uint8)
        self.assertIs(self.limit(white, 100, 1.0)[0], white)

    def test_colours_keep_their_proportions(self):
        frame = np.zeros((32, 128, 3), dtype=np.uint8)
        frame[...] = (240, 120, 60)
        out, _ = self.limit(frame, 100, 0.1)
        r, g, b = (float(out[..., c].mean()) for c in range(3))
        self.assertAlmostEqual(r / g, 2.0, delta=0.1)
        self.assertAlmostEqual(g / b, 2.0, delta=0.15)


if __name__ == "__main__":
    unittest.main()
