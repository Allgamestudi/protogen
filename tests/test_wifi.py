"""Wi-Fi set-up: reading nmcli, checking input, the page's routes. No real network is touched.

Run from the project root: python -B -m unittest discover -s tests -v
"""
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pi"))
import test_web                                                     # noqa: E402,F401  (sets up the fakes)
import web_app                                                      # noqa: E402
import wifi                                                         # noqa: E402


class ParsingTests(unittest.TestCase):
    def test_terse_lines(self):
        self.assertEqual(wifi.split_terse(r"wlan0:wifi:connected:My\:Net"), ["wlan0", "wifi", "connected", "My:Net"])
        self.assertEqual(wifi.split_terse("a::c"), ["a", "", "c"])

    def test_devices(self):
        wifi_on = "lo:loopback:connected (externally):lo\nwlan0:wifi:connected:Home\neth0:ethernet:unavailable:\n"
        self.assertEqual(wifi.parse_devices(wifi_on), ("wifi", "Home"))
        self.assertEqual(wifi.parse_devices("wlan0:wifi:disconnected:\neth0:ethernet:connected:Wired 1\n"),
                         ("ethernet", None))
        self.assertEqual(wifi.parse_devices("wlan0:wifi:disconnected:\nlo:loopback:unmanaged:\n"), ("none", None))

    def test_scan_keeps_the_strongest_of_each_name(self):
        text = " :Home:40:WPA2\n*:Home:71:WPA2\n :Cafe:55:\n ::30:WPA2\n :Far:12:WPA1 WPA2\n"
        nets = wifi.parse_scan(text)
        self.assertEqual([n["ssid"] for n in nets], ["Home", "Cafe", "Far"])
        self.assertEqual(nets[0], {"ssid": "Home", "signal": 71, "secure": True, "active": True})
        self.assertFalse(nets[1]["secure"])

    def test_input(self):
        self.assertEqual(wifi.check_input("Home", "12345678"), "")
        self.assertEqual(wifi.check_input("Open", ""), "")
        self.assertEqual(wifi.check_input("", "12345678"), "bad_ssid")
        self.assertEqual(wifi.check_input("x" * 33, "12345678"), "bad_ssid")
        self.assertEqual(wifi.check_input("Home", "short"), "bad_password")
        self.assertEqual(wifi.check_input("Home", "x" * 65), "bad_password")
        self.assertEqual(wifi.check_input(None, "12345678"), "bad_input")
        self.assertEqual(wifi.check_input("Ho\nme", "12345678"), "bad_ssid")

    def test_reasons(self):
        self.assertEqual(wifi.failure_reason("Error: Connection activation failed: Secrets were required, "
                                             "but not provided."), "wrong_password")
        self.assertEqual(wifi.failure_reason("Error: No network with SSID 'x' found."), "not_found")
        self.assertEqual(wifi.failure_reason("something else"), "failed")


class RealPathTests(unittest.TestCase):
    """The nmcli path, with nmcli itself replaced."""

    def setUp(self):
        wifi.fake = None
        wifi._job.update(state="idle", ssid="", reason="")

    def test_no_wifi_is_reported_without_probing_the_internet(self):
        with patch.object(wifi, "available", return_value=True), \
                patch.object(wifi, "_nmcli", return_value=(True, "wlan0:wifi:disconnected:\n", "")), \
                patch.object(wifi, "has_internet") as probe:
            self.assertEqual(wifi.status()["state"], "no_wifi")
            probe.assert_not_called()

    def test_wifi_without_internet(self):
        with patch.object(wifi, "available", return_value=True), \
                patch.object(wifi, "_nmcli", return_value=(True, "wlan0:wifi:connected:Home\n", "")), \
                patch.object(wifi, "has_internet", return_value=False):
            s = wifi.status()
        self.assertEqual((s["state"], s["ssid"], s["link"]), ("no_internet", "Home", "wifi"))

    def test_a_failed_join_removes_its_profile_and_goes_back(self):
        calls = []

        def nmcli(*args, timeout=15):
            calls.append(args)
            if args[:2] == ("-t", "-f") and "device" in args:
                return True, "wlan0:wifi:connected:Old\n", ""
            if "show" in args:
                return True, "Old\n", ""
            if "connect" in args:
                return False, "", "Error: Secrets were required, but not provided."
            return True, "", ""

        with patch.object(wifi, "available", return_value=True), patch.object(wifi, "_nmcli", side_effect=nmcli):
            self.assertEqual(wifi.connect("New", "wrong-pass"), {"ok": True})
            for _ in range(100):
                if wifi._job["state"] != "connecting":
                    break
                time.sleep(0.01)
        self.assertEqual((wifi._job["state"], wifi._job["reason"]), ("error", "wrong_password"))
        self.assertIn(("connection", "delete", "id", "New"), calls)
        self.assertIn(("connection", "up", "id", "Old"), calls)
        join = next(c for c in calls if "connect" in c)
        self.assertEqual(join[-3:], ("New", "password", "wrong-pass"))


class PageTests(unittest.TestCase):
    """What the page sees, with the simulator's pretend network."""

    def setUp(self):
        wifi.fake = {"net": "online", "ssid": "Home"}
        wifi._job.update(state="idle", ssid="", reason="")

    def tearDown(self):
        wifi.fake = None

    def call(self, method, path, body=None):
        return web_app.app.call(method, path, json_body=body).get_json()

    def wait(self):
        for _ in range(300):
            job = self.call("GET", "/api/wifi")["job"]
            if job["state"] != "connecting":
                return job
            time.sleep(0.01)
        self.fail("still connecting")

    def test_update_says_what_is_missing(self):
        with patch.object(web_app.updater, "check", return_value={"ok": True, "behind": 0}) as check:
            self.assertTrue(self.call("POST", "/api/system/update/check", {})["ok"])
            for net in ("no_wifi", "no_internet"):
                wifi.fake["net"] = net
                j = self.call("POST", "/api/system/update/check", {})
                self.assertEqual((j["ok"], j["reason"]), (False, net))
            self.assertEqual(check.call_count, 1)           # the server is not asked without a network
        with patch.object(web_app.updater, "start") as start:
            self.assertEqual(self.call("POST", "/api/system/update", {})["reason"], "no_internet")
            start.assert_not_called()

    def test_connect_from_the_page(self):
        wifi.fake["net"] = "no_wifi"
        self.assertEqual(self.call("GET", "/api/wifi")["state"], "no_wifi")
        self.assertEqual(len(self.call("GET", "/api/wifi/scan")["networks"]), 3)
        with patch("time.sleep"):
            r = web_app.app.call("POST", "/api/wifi/connect", json_body={"ssid": "Home", "password": "secret-123"})
            self.assertEqual(r.get_json(), {"ok": True})
            self.assertEqual(self.wait()["state"], "ok")
        s = self.call("GET", "/api/wifi")
        self.assertEqual((s["state"], s["ssid"]), ("online", "Home"))
        self.assertNotIn("secret-123", str(s))              # the password never comes back

    def test_bad_input_is_refused_at_once(self):
        self.assertEqual(self.call("POST", "/api/wifi/connect", {"ssid": "Home", "password": "123"}),
                         {"ok": False, "reason": "bad_password"})
        self.assertEqual(self.call("POST", "/api/wifi/connect", {}), {"ok": False, "reason": "bad_input"})
        with patch("time.sleep"):
            self.call("POST", "/api/wifi/connect", {"ssid": "Nowhere", "password": "12345678"})
            self.assertEqual(self.wait()["reason"], "not_found")


if __name__ == "__main__":
    unittest.main()
