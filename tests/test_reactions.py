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


class ClapTests(unittest.TestCase):
    BANDS = 16

    def flat(self, lv):
        return np.full(self.BANDS, lv, dtype=np.float32)

    def run_frames(self, d, frames, t0=0.0, sens=0.5):
        hits, t = [], t0
        for f in frames:
            if d.update(t, f, DT, sens):
                hits.append(round(t, 2))
            t += DT
        return hits, t

    def noise(self, mean, sd, n, seed):
        rng = np.random.default_rng(seed)
        return [np.clip(mean + sd * rng.standard_normal(self.BANDS), 0, 1) for _ in range(n)]

    def test_clap_in_a_quiet_room(self):
        d = reactions.ClapDetector()
        quiet = self.noise(0.03, 0.005, 90, 1)
        hits, t = self.run_frames(d, quiet)
        self.assertEqual(hits, [])
        hits, t = self.run_frames(d, [self.flat(0.35), self.flat(0.2), self.flat(0.06)] + quiet[:30], t)
        self.assertEqual(len(hits), 1)

    def test_clap_in_a_loud_hall(self):
        d = reactions.ClapDetector()
        hall = self.noise(0.30, 0.03, 120, 2)
        hits, t = self.run_frames(d, hall)
        self.assertEqual(hits, [], "steady noise is not a clap")
        hits, _ = self.run_frames(d, [self.flat(0.95), self.flat(0.6)] + hall[:20], t)
        self.assertEqual(len(hits), 1)

    def test_a_voice_after_a_pause_is_not_a_clap(self):
        d = reactions.ClapDetector()
        i = np.arange(self.BANDS)
        voice = (0.4 * np.exp(-((i - 5.0) / 2.2) ** 2)).astype(np.float32)   # middle bands only
        frames = self.noise(0.02, 0.003, 90, 3) + [voice] * 8 + self.noise(0.02, 0.003, 30, 4) + [voice * 1.5] * 8
        hits, _ = self.run_frames(d, frames)
        self.assertEqual(hits, [])

    def test_music_getting_louder_slowly_is_not_a_clap(self):
        d = reactions.ClapDetector()
        hits, _ = self.run_frames(d, [self.flat(v) for v in np.linspace(0.05, 0.6, 300)])
        self.assertEqual(hits, [])

    def test_one_reaction_per_clap_and_sensitivity(self):
        d = reactions.ClapDetector()
        hits, t = self.run_frames(d, [self.flat(0.03)] * 60 + [self.flat(0.4)] * 4 + [self.flat(0.03)] * 30)
        self.assertEqual(len(hits), 1)
        soft = [self.flat(0.03)] * 60 + [self.flat(0.085)] + [self.flat(0.03)] * 30
        self.assertEqual(self.run_frames(reactions.ClapDetector(), soft, sens=0.0)[0], [])
        self.assertEqual(len(self.run_frames(reactions.ClapDetector(), soft, sens=1.0)[0]), 1)


class ToFCalibratorTests(unittest.TestCase):
    def feed(self, c, readings, t0=0.0, step=0.08):
        t = t0
        for mm in readings:
            c.update(t, mm, mm is not None)
            t += step
        return t

    def test_finds_the_visor_reflection_by_itself(self):
        c = reactions.ToFCalibrator()
        self.feed(c, [42, 44, 43, 41, 43] * 60)                   # 24 s of a reading that never changes
        self.assertEqual(c.phantom, 43)
        self.assertEqual(c.min_valid(50), 53)

    def test_a_moving_person_is_not_a_reflection(self):
        c = reactions.ToFCalibrator()
        self.feed(c, [300 + (i * 37) % 200 for i in range(300)])
        self.assertIsNone(c.phantom)
        self.assertEqual(c.min_valid(50), 50)

    def test_calibrate_now(self):
        c = reactions.ToFCalibrator()
        c.calibrate(0.0)
        self.assertTrue(c.calibrating)
        self.feed(c, [38, 39, 38, 40] * 20, step=0.08)            # 6.4 s
        self.assertFalse(c.calibrating)
        self.assertEqual(c.phantom, 38)
        c.calibrate(100.0)
        self.feed(c, [None] * 70, t0=100.0)                         # nothing in range: no reflection
        self.assertIsNone(c.phantom)

    def test_reflection_that_went_away(self):
        c = reactions.ToFCalibrator()
        t = self.feed(c, [60] * 300)
        self.assertEqual(c.phantom, 60)
        self.feed(c, [400 + (i % 7) for i in range(300)], t0=t)
        self.assertIsNone(c.phantom)


class BoopSensorTests(unittest.TestCase):
    def test_finger_at_the_nose(self):
        b = reactions.BoopSensor()
        self.assertFalse(b.update(0.0, 200, True))
        self.assertFalse(b.update(0.1, 45, True))
        self.assertTrue(b.update(0.2, 25, True))

    def test_sensor_blinded_by_the_finger(self):
        b = reactions.BoopSensor()
        b.update(0.0, 50, True)                       # coming close...
        self.assertTrue(b.update(0.1, None, False))   # ...then too close to measure
        self.assertTrue(b.update(3.0, None, False))
        self.assertFalse(b.update(7.0, None, False))  # blind for too long: not a finger any more
        self.assertFalse(b.update(7.1, 300, True))

    def test_out_of_range_far_away_is_not_a_boop(self):
        b = reactions.BoopSensor()
        b.update(0.0, 900, True)
        self.assertFalse(b.update(0.1, None, False))

    def test_visor_reflection_is_not_a_finger(self):
        b = reactions.BoopSensor()
        self.assertFalse(b.update(0.0, 28, True, boop_mm=30, phantom=27))
        self.assertTrue(b.update(0.1, 15, True, boop_mm=30, phantom=27))

    def test_boop_and_pet_through_gestures(self):
        g = reactions.Gestures()
        b = reactions.BoopSensor()
        t, events = 0.0, []
        for mm, ok in [(200, True), (40, True), (None, False), (None, False), (200, True)]:
            _, ev = g.update(t, near=b.update(t, mm, ok))
            events.append(ev)
            t += 0.1
        self.assertIn("boop", events)


if __name__ == "__main__":
    unittest.main()
