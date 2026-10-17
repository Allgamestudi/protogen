"""Saved Wi-Fi networks and connecting the copy to the update repository. Nothing real is run.

Run from the project root: python -B -m unittest discover -s tests -v
"""
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pi"))
import test_web                                                     # noqa: E402,F401  (sets up the fakes)
import updater                                                      # noqa: E402
import web_app                                                      # noqa: E402
import wifi                                                         # noqa: E402

TOKEN = "github_pat_" + "A" * 40


class SavedNetworkTests(unittest.TestCase):
    def setUp(self):
        wifi.fake = {"net": "online", "ssid": "Home", "saved": ["Home", "Dacha"]}

    def tearDown(self):
        wifi.fake = None

    def call(self, path, body=None):
        method = "GET" if body is None else "POST"
        return web_app.app.call(method, path, json_body=body).get_json()

    def test_list_marks_the_network_in_use(self):
        nets = self.call("/api/wifi/saved")["networks"]
        self.assertEqual(nets, [{"ssid": "Home", "active": True}, {"ssid": "Dacha", "active": False}])

    def test_add_and_forget(self):
        self.assertEqual(self.call("/api/wifi/add", {"ssid": "Work", "password": "12345678"}), {"ok": True})
        self.assertIn("Work", [n["ssid"] for n in self.call("/api/wifi/saved")["networks"]])
        self.assertEqual(self.call("/api/wifi/forget", {"ssid": "Work"}), {"ok": True})
        self.assertNotIn("Work", [n["ssid"] for n in self.call("/api/wifi/saved")["networks"]])

    def test_the_network_in_use_cannot_be_forgotten(self):
        self.assertEqual(self.call("/api/wifi/forget", {"ssid": "Home"}), {"ok": False, "reason": "active"})

    def test_bad_input(self):
        self.assertEqual(self.call("/api/wifi/add", {"ssid": "Work", "password": "123"})["reason"], "bad_password")
        self.assertEqual(self.call("/api/wifi/forget", {"ssid": "Nope"})["reason"], "not_found")


class NmcliTests(unittest.TestCase):
    def setUp(self):
        wifi.fake = None

    def test_saved_reads_only_wifi_profiles(self):
        out = "Home:802-11-wireless:yes\nWired connection 1:802-3-ethernet:no\nDacha\\:2:802-11-wireless:no\n"
        with patch.object(wifi, "_nmcli", return_value=(True, out, "")):
            self.assertEqual(wifi.saved(), [{"ssid": "Home", "active": True}, {"ssid": "Dacha:2", "active": False}])

    def test_add_builds_a_profile_and_keeps_the_password_out_of_the_answer(self):
        calls = []

        def nmcli(*args, timeout=15):
            calls.append(args)
            return (True, "", "") if "add" in args else (True, "Home\n", "")

        with patch.object(wifi, "available", return_value=True), patch.object(wifi, "_nmcli", side_effect=nmcli):
            answer = wifi.add("Work", "secret-123")
        self.assertEqual(answer, {"ok": True})
        add = next(c for c in calls if "add" in c)
        self.assertEqual(add[:6], ("connection", "add", "type", "wifi", "con-name", "Work"))
        self.assertIn("wifi-sec.psk", add)
        self.assertNotIn("secret-123", str(answer))


class UpdateSetupTests(unittest.TestCase):
    def test_token_is_hidden_everywhere(self):
        url = updater.with_token("https://github.com/Allgamestudi/protogen.git", TOKEN)
        self.assertTrue(url.startswith("https://token:" + TOKEN + "@github.com/"))
        self.assertEqual(updater.hide_token("fatal: repository '" + url + "' not found"),
                         "fatal: repository 'https://***@github.com/Allgamestudi/protogen.git' not found")
        self.assertEqual(updater.with_token(url, ""), url)

    def test_address_and_token_are_checked(self):
        self.assertEqual(updater.check_setup("https://github.com/Allgamestudi/protogen.git", ""), "")
        self.assertEqual(updater.check_setup("https://github.com/Allgamestudi/protogen", TOKEN), "")
        self.assertEqual(updater.check_setup("http://github.com/a/b.git", ""), "bad_url")
        self.assertEqual(updater.check_setup("https://github.com/a/b.git; rm -rf ~", ""), "bad_url")
        self.assertEqual(updater.check_setup("https://github.com/a/b.git", "short"), "bad_token")
        self.assertEqual(updater.check_setup("https://github.com/a/b.git", "x" * 30 + " y"), "bad_token")

    def test_setup_runs_the_script_and_reports(self):
        with patch.object(updater, "version", return_value={"repo": False}), \
                patch.object(updater, "status", return_value={"state": "idle"}), \
                patch.object(updater, "_write_status") as write, \
                patch.object(updater.subprocess, "run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "this copy is now at abc1234  2026-10-18  firmware\n"
            run.return_value.stderr = ""
            self.assertEqual(updater.setup("https://github.com/Allgamestudi/protogen.git", TOKEN), {"ok": True})
            for _ in range(200):
                if write.call_count >= 2:
                    break
                time.sleep(0.01)
        cmd = run.call_args[0][0]
        self.assertTrue(cmd[-3].endswith("enable_updates.sh"))
        self.assertIn(TOKEN, cmd[-2])                              # the token goes to git...
        state, message = write.call_args[0]
        self.assertEqual(state, "done")
        self.assertNotIn(TOKEN, message)                           # ...and never back to the page

    def test_builtin_token_is_used_when_none_is_given(self):
        with patch.object(updater, "builtin_token", return_value=TOKEN),                 patch.object(updater, "version", return_value={"repo": False}),                 patch.object(updater, "status", return_value={"state": "idle"}),                 patch.object(updater.threading, "Thread") as thread:
            self.assertEqual(updater.setup("https://github.com/Allgamestudi/protogen.git"), {"ok": True})
        self.assertEqual(thread.call_args[1]["args"][1], TOKEN)

    def test_already_a_clone(self):
        with patch.object(updater, "version", return_value={"repo": True}):
            self.assertEqual(updater.setup("https://github.com/a/b.git")["reason"], "already")

    def test_simulator_refuses(self):
        with patch.object(web_app, "SIMULATED", True):
            answer = web_app.app.call("POST", "/api/system/update/setup", json_body={"url": "https://github.com/a/b.git"})
        self.assertFalse(answer.get_json()["ok"])


if __name__ == "__main__":
    unittest.main()
