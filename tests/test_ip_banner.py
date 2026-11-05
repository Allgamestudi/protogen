"""The face shows the Pi's address after it joins a network. Nothing real is run.

Run from the project root: python -B -m unittest discover -s tests -v
"""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pi"))
import test_web                                                     # noqa: E402,F401  (sets up the fakes)
import numpy as np                                                  # noqa: E402
import config                                                       # noqa: E402
import ip_banner                                                    # noqa: E402

IP_OUT = """1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever
3: wlan0    inet 192.168.43.120/24 brd 192.168.43.255 scope global dynamic noprefixroute wlan0\\       valid_lft 3590sec
2: eth0    inet 10.0.0.5/24 brd 10.0.0.255 scope global eth0\\       valid_lft forever
"""


class ParseTests(unittest.TestCase):
    def test_wifi_address_first(self):
        found = ip_banner.parse_ips(IP_OUT)
        self.assertNotIn(("lo", "127.0.0.1"), found)
        self.assertEqual(ip_banner.pick(found), "192.168.43.120")

    def test_cable_when_no_wifi(self):
        self.assertEqual(ip_banner.pick(ip_banner.parse_ips(IP_OUT.splitlines()[2])), "10.0.0.5")

    def test_own_hotspot_is_not_shown(self):
        out = "3: wlan0    inet 10.42.0.1/24 brd 10.42.0.255 scope global noprefixroute wlan0"
        self.assertIsNone(ip_banner.pick(ip_banner.parse_ips(out)))

    def test_nothing(self):
        self.assertIsNone(ip_banner.pick(ip_banner.parse_ips("")))


class BannerTests(unittest.TestCase):
    def setUp(self):
        self.b = ip_banner.Banner(30)

    def test_shows_after_joining_then_hides(self):
        self.assertIsNone(self.b.text(0))
        self.b.step(10, "192.168.1.5")
        self.assertEqual(self.b.text(10), "192.168.1.5")
        self.assertEqual(self.b.text(39), "192.168.1.5")
        self.assertIsNone(self.b.text(40.1))

    def test_same_address_does_not_show_again(self):
        self.b.step(0, "192.168.1.5")
        self.b.step(100, "192.168.1.5")
        self.assertIsNone(self.b.text(101))

    def test_new_network_shows_again(self):
        self.b.step(0, "192.168.1.5")
        self.b.step(100, None)                      # lost the network
        self.assertIsNone(self.b.text(101))
        self.b.step(200, "192.168.43.7")
        self.assertEqual(self.b.text(201), "192.168.43.7")

    def test_show_again(self):
        self.b.step(0, "192.168.1.5")
        self.b.show_again(500)
        self.assertEqual(self.b.text(510), "192.168.1.5")

    def test_port_when_plain_port_is_off(self):
        with patch.object(config, "WEB_PORT_PLAIN", 0, create=True):
            self.assertEqual(ip_banner.label("192.168.1.5"), f"192.168.1.5:{config.WEB_PORT}")
        with patch.object(config, "WEB_PORT_PLAIN", 80, create=True):
            self.assertEqual(ip_banner.label("192.168.1.5"), "192.168.1.5")


class DrawTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib
        from types import ModuleType
        stubs = {"rgbmatrix": ModuleType("rgbmatrix"), "serial_proto": ModuleType("serial_proto")}
        stubs["rgbmatrix"].RGBMatrix = stubs["rgbmatrix"].RGBMatrixOptions = object
        with patch.dict(sys.modules, stubs), patch("os.makedirs"):
            cls.draw = staticmethod(importlib.import_module("matrix_renderer")._draw_ip)

    def test_fits_on_two_panels_and_is_white(self):
        frame = np.full((32, 128, 3), 120, np.uint8)
        out = self.draw(frame.copy(), "192.168.100.200", 0.0)
        self.assertEqual(out.shape, frame.shape)
        self.assertTrue((out == 255).all(axis=2).any())                  # the text is there
        self.assertEqual(int(out[0, 0, 0]), 120)                         # the face above the band is untouched
        self.assertLess(int(out[16, 0, 0]), 120)                         # and dimmed behind the text

    def test_scrolls_on_one_panel(self):
        frame = np.zeros((32, 64, 3), np.uint8)
        a = self.draw(frame.copy(), "192.168.100.200:5001", 0.0)
        b = self.draw(frame.copy(), "192.168.100.200:5001", 1.0)
        self.assertFalse(np.array_equal(a, b))


if __name__ == "__main__":
    unittest.main()
