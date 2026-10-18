"""Own Wi-Fi network: when it opens and closes, how the page sees it. Nothing real is run.

Run from the project root: python -B -m unittest discover -s tests -v
"""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pi"))
import test_web                                                     # noqa: E402,F401  (sets up the fakes)
import hotspot                                                      # noqa: E402
import web_app                                                      # noqa: E402
import wifi                                                         # noqa: E402


class KeeperTests(unittest.TestCase):
    def test_opens_after_a_minute_without_a_network(self):
        k = hotspot.Keeper()
        self.assertIsNone(k.step(0, "none", False, False, 0))
        self.assertIsNone(k.step(hotspot.START_AFTER_S - 1, "none", False, False, 0))
        self.assertEqual(k.step(hotspot.START_AFTER_S, "none", False, False, 0), "up")

    def test_never_opens_with_a_network_or_a_cable(self):
        k = hotspot.Keeper()
        for link in ("wifi", "ethernet"):
            for t in range(0, 600, 5):
                self.assertIsNone(k.step(t, link, False, False, 0))

    def test_stays_while_a_phone_is_on_it(self):
        k = hotspot.Keeper()
        k.step(0, "none", False, False, 0)
        k.step(60, "none", False, False, 0)
        for t in range(65, 2000, 5):
            self.assertIsNone(k.step(t, "hotspot", False, True, 1))

    def test_looks_for_known_networks_when_nobody_uses_it(self):
        k = hotspot.Keeper()
        k.step(0, "none", False, False, 0)
        self.assertEqual(k.step(60, "none", False, False, 0), "up")
        self.assertIsNone(k.step(65, "hotspot", False, True, 0))
        self.assertEqual(k.step(60 + hotspot.RETRY_S, "hotspot", False, True, 0), "down")
        # nothing found: back up after a shorter look
        t = 60 + hotspot.RETRY_S + 5
        self.assertIsNone(k.step(t, "none", False, False, 0))
        self.assertEqual(k.step(t + hotspot.LOOK_S, "none", False, False, 0), "up")

    def test_leaves_a_connection_from_the_page_alone(self):
        k = hotspot.Keeper()
        for t in range(0, 600, 5):
            self.assertIsNone(k.step(t, "none", True, False, 0))


class StatusTests(unittest.TestCase):
    def setUp(self):
        wifi.fake = None

    def test_own_network_shows_as_hotspot(self):
        devices = "wlan0:wifi:connected:" + wifi.AP_CON + "\n"
        with patch.object(wifi, "available", return_value=True), \
                patch.object(wifi, "_nmcli", return_value=(True, devices, "")):
            st = wifi.status(check_internet=False)
        self.assertEqual((st["link"], st["state"], st["ssid"]), ("hotspot", "no_wifi", "Protogen"))

    def test_own_network_is_not_a_saved_one(self):
        out = "Home:802-11-wireless:no\n" + wifi.AP_CON + ":802-11-wireless:yes\n"
        with patch.object(wifi, "_nmcli", return_value=(True, out, "")):
            self.assertEqual([n["ssid"] for n in wifi.saved()], ["Home"])


class PortalTests(unittest.TestCase):
    def test_phone_checks_lead_to_the_page(self):
        for path in ("/generate_204", "/hotspot-detect.html", "/connecttest.txt"):
            r = web_app.app.call("GET", path)
            self.assertEqual(r.status, 302, path)
            self.assertEqual(r.headers["Location"], "http://10.42.0.1/")


if __name__ == "__main__":
    unittest.main()
