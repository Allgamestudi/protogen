"""Own Wi-Fi network: when it opens and closes, how the page sees it. Nothing real is run.

Run from the project root: python -B -m unittest discover -s tests -v
"""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import os
import tempfile

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


class SettingsTests(unittest.TestCase):
    """Name and password of our own network, set on the page."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.p = patch.object(hotspot, "CONF_FILE", os.path.join(self.dir.name, "hotspot.json"))
        self.p.start()
        self.a = patch.object(hotspot, "active", return_value=False)
        self.a.start()

    def tearDown(self):
        self.a.stop()
        self.p.stop()
        self.dir.cleanup()

    def test_default_is_protogen_protogen(self):
        self.assertEqual(hotspot.settings(), {"ssid": "Protogen", "password": "protogen"})
        self.assertFalse(hotspot.public()["custom"])

    def test_save_and_reset(self):
        self.assertTrue(hotspot.save("  My head ", "secret-123")["ok"])
        self.assertEqual(hotspot.settings(), {"ssid": "My head", "password": "secret-123"})
        self.assertTrue(hotspot.public()["custom"])
        self.assertTrue(hotspot.reset()["ok"])
        self.assertEqual(hotspot.ssid(), "Protogen")
        self.assertFalse(os.path.exists(hotspot.CONF_FILE))

    def test_saving_the_defaults_leaves_no_file(self):
        hotspot.save("Protogen", "protogen")
        self.assertFalse(os.path.exists(hotspot.CONF_FILE))

    def test_bad_input_is_refused(self):
        self.assertEqual(hotspot.save("", "secret-123")["reason"], "bad_ssid")
        self.assertEqual(hotspot.save("X" * 33, "secret-123")["reason"], "bad_ssid")
        self.assertEqual(hotspot.save("Head", "")["reason"], "bad_password")       # always a password
        self.assertEqual(hotspot.save("Head", "short")["reason"], "bad_password")
        self.assertEqual(hotspot.save("Head", "x" * 64)["reason"], "bad_password")
        self.assertEqual(hotspot.ssid(), "Protogen")

    def test_broken_file_falls_back_to_defaults(self):
        for text in ("{not json", '{"ssid": "", "password": "secret-123"}', '{"ssid": "A", "password": "123"}'):
            with open(hotspot.CONF_FILE, "w", encoding="utf-8") as f:
                f.write(text)
            self.assertEqual(hotspot.settings(), hotspot.defaults(), text)

    def test_status_shows_the_name_from_the_page(self):
        hotspot.save("My head", "secret-123")
        wifi.fake = None
        devices = "wlan0:wifi:connected:" + wifi.AP_CON + "\n"
        with patch.object(wifi, "available", return_value=True), \
                patch.object(wifi, "_nmcli", return_value=(True, devices, "")):
            self.assertEqual(wifi.status(check_internet=False)["ssid"], "My head")

    def test_running_network_restarts_with_new_settings(self):
        with patch.object(hotspot, "active", return_value=True), \
                patch.object(hotspot, "down") as down, patch.object(hotspot, "up") as up:
            r = hotspot.save("My head", "secret-123")
            for th in __import__("threading").enumerate():
                if th.name == "hotspot-restart":
                    th.join(2)
        self.assertTrue(r["ok"])
        down.assert_called_once()
        up.assert_called_once()

    def test_page_api(self):
        r = web_app.app.call("POST", "/api/hotspot", json_body={"ssid": "My head", "password": "secret-123"}).get_json()
        self.assertTrue(r["ok"])
        self.assertEqual(web_app.app.call("GET", "/api/hotspot").get_json()["ssid"], "My head")
        r = web_app.app.call("POST", "/api/hotspot", json_body={"ssid": "My head", "password": 12345678}).get_json()
        self.assertEqual(r["reason"], "bad_password")
        self.assertTrue(web_app.app.call("POST", "/api/hotspot/reset", json_body={}).get_json()["ok"])
        self.assertEqual(web_app.app.call("GET", "/api/hotspot").get_json()["ssid"], "Protogen")


class ForeignHotspotTests(unittest.TestCase):
    """A hotspot profile made by hand (e.g. 'proto') is removed: only ours is shared."""

    def test_finds_and_removes_only_foreign_access_points(self):
        conns = ("Home:802-11-wireless\n" + hotspot.CON + ":802-11-wireless\n"
                 "proto:802-11-wireless\nWired connection 1:802-3-ethernet\n")
        modes = {"Home": "infrastructure", "proto": "ap"}
        calls = []

        def run(*cmd, timeout=30):
            calls.append(cmd)
            if cmd[:3] == ("nmcli", "-t", "-f"):
                return True, conns
            if "802-11-wireless.mode" in cmd:
                return True, modes.get(cmd[-1], "") + "\n"
            return True, ""

        with patch.object(hotspot, "_run", side_effect=run), patch.object(hotspot, "log_err"):
            self.assertEqual(hotspot.foreign_aps(), ["proto"])
            hotspot.remove_foreign_aps()
        deleted = [c[-1] for c in calls if c[:3] == ("nmcli", "connection", "delete")]
        self.assertEqual(deleted, ["proto"])

    def test_nothing_when_nmcli_fails(self):
        with patch.object(hotspot, "_run", return_value=(False, "")):
            self.assertEqual(hotspot.foreign_aps(), [])


if __name__ == "__main__":
    unittest.main()
