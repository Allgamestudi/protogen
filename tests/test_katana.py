"""The katana prop switches the face while its blade is lit.

Run from the project root: python -B -m unittest discover -s tests -v
"""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pi"))
import test_web                                                     # noqa: E402,F401  (sets up the fakes)
import emotions                                                     # noqa: E402
import web_app                                                      # noqa: E402
from state import state, lock                                       # noqa: E402


def call(method, path, body=None):
    return web_app.app.call(method, path, json_body=body).get_json()


class KatanaTests(unittest.TestCase):
    def setUp(self):
        with lock:
            state.update(mode="emotion", emotion="love", effect="rainbow", color=[1, 2, 3])
        web_app._katana.update(active=False, saved=None)

    def test_blade_on_shows_the_first_emotion_in_its_colour(self):
        self.assertTrue(call("POST", "/api/katana", {"on": True, "color": [255, 0, 0]})["active"])
        self.assertEqual(state["emotion"], emotions.list_emotions()[0]["id"])
        self.assertEqual((state["effect"], state["color"]), ("solid", [255, 0, 0]))

    def test_colour_follows_while_lit(self):
        call("POST", "/api/katana", {"on": True, "color": [255, 0, 0]})
        call("POST", "/api/katana", {"on": True, "color": [0, 220, 255]})
        self.assertEqual(state["color"], [0, 220, 255])

    def test_blade_off_brings_the_face_back(self):
        call("POST", "/api/katana", {"on": True, "color": [255, 0, 0]})
        call("POST", "/api/katana", {"on": True, "color": [0, 0, 255]})
        self.assertFalse(call("POST", "/api/katana", {"on": False})["active"])
        self.assertEqual((state["emotion"], state["effect"], state["color"]), ("love", "rainbow", [1, 2, 3]))

    def test_off_without_on_changes_nothing(self):
        call("POST", "/api/katana", {"on": False})
        self.assertEqual(state["emotion"], "love")

    def test_bad_colour_is_ignored(self):
        call("POST", "/api/katana", {"on": True, "color": [999, "x", 0]})
        self.assertEqual(state["color"], [1, 2, 3])

    def test_status(self):
        self.assertEqual(call("GET", "/api/katana")["active"], False)


if __name__ == "__main__":
    unittest.main()
