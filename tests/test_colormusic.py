"""Colour music: pure NumPy, no hardware.

Run from the project root: python -B -m unittest discover -s tests -v
"""
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pi"))
import colormusic                                                   # noqa: E402

DT = 1.0 / 30.0
LOUD = np.full(16, 0.8, np.float32)
QUIET = np.zeros(16, np.float32)


def play(show, bars, style, seconds=1.0, beat_every=0):
    frame = None
    for n in range(int(seconds / DT)):
        beat = 1.0 if beat_every and n % beat_every == 0 else 0.0
        frame = show.frame(bars, beat, DT, style)
    return frame


class ColourMusicTests(unittest.TestCase):
    def test_every_style_gives_a_panel_frame(self):
        for style in colormusic.STYLES:
            frame = play(colormusic.Show(64, 32), LOUD, style, 0.5, beat_every=8)
            self.assertEqual((frame.shape, frame.dtype), ((32, 64, 3), np.uint8), style)
            self.assertGreater(int(frame.max()), 80, style)

    def test_silence_is_dark(self):
        for style in ("zones", "flash", "rings"):
            frame = play(colormusic.Show(), QUIET, style, 2.0)
            self.assertLess(float(frame.mean()), 6.0, style)

    def test_zones_are_bass_red_middle_green_treble_blue(self):
        for band, channel, column in ((slice(0, 4), 0, 8), (slice(4, 10), 1, 32), (slice(10, 16), 2, 56)):
            bars = QUIET.copy()
            bars[band] = 1.0
            frame = play(colormusic.Show(), bars, "zones")
            self.assertEqual(int(np.argmax(frame[-1, column])), channel)
            self.assertGreater(int(frame[-1, column, channel]), 150)

    def test_levels_rise_fast_and_fall_slowly(self):
        show = colormusic.Show()
        play(show, LOUD, "zones", 0.2)
        self.assertGreater(float(show.level[0]), 0.7)
        play(show, QUIET, "zones", 0.2)
        self.assertGreater(float(show.level[0]), 0.2)      # still fading, no flicker

    def test_a_beat_sends_a_ring_outwards(self):
        show = colormusic.Show()
        show.frame(QUIET, 1.0, DT, "rings")
        first = show.rings[0][0]
        play(show, QUIET, "rings", 0.3)
        self.assertGreater(show.rings[0][0], first + 8)
        play(show, QUIET, "rings", 4.0)
        self.assertEqual(show.rings, [])                    # gone past the edge

    def test_rings_do_not_pile_up(self):
        show = colormusic.Show()
        for _ in range(100):
            show.frame(LOUD, 1.0, DT, "rings")
        self.assertLessEqual(len(show.rings), colormusic.MAX_RINGS)

    def test_auto_moves_through_the_styles(self):
        show = colormusic.Show()
        seen = set()
        for n in range(30 * 20):
            show.frame(LOUD, 1.0 if n % 10 == 0 else 0.0, DT, "auto")
            seen.add(show.auto_at)
        self.assertEqual(seen, set(range(len(colormusic.AUTO_ORDER))))

    def test_unknown_style_and_other_band_counts(self):
        show = colormusic.Show(64, 32)
        self.assertEqual(show.frame(np.full(8, 0.5, np.float32), 0.0, DT, "nonsense").shape, (32, 64, 3))
        self.assertEqual(colormusic.render(LOUD).shape, (32, 64, 3))      # the short call still works


if __name__ == "__main__":
    unittest.main()
