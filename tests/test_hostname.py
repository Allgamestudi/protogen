"""The address http://proto.local: when the service has to put the name right. Nothing is run.

Run from the project root: python -B -m unittest discover -s tests -v
"""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pi"))
import test_web                                                     # noqa: E402,F401  (sets up the fakes)
import hostname_guard as guard                                      # noqa: E402
import web_app                                                      # noqa: E402


class NameTests(unittest.TestCase):
    def name(self, saved, default="proto"):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "hostname"
            if saved is not None:
                path.write_text(saved, encoding="utf-8")
            return guard.wanted_name(str(path), default)

    def test_default_is_proto(self):
        self.assertEqual(self.name(None), "proto")
        self.assertEqual(guard.config.HOSTNAME, "proto")

    def test_a_saved_name_wins(self):
        self.assertEqual(self.name("Foxy-2\n"), "foxy-2")

    def test_a_broken_saved_name_is_ignored(self):
        for bad in ("", "has space", "-lead", "trail-", "под", "a" * 64, "x;reboot"):
            self.assertEqual(self.name(bad), "proto", bad)

    def test_empty_default_means_hands_off(self):
        self.assertEqual(self.name(None, ""), "")
        self.assertEqual(self.name(None, "Bad Name"), "")

    def test_when_a_fix_is_needed(self):
        self.assertFalse(guard.needs_fix("proto", "proto", True))
        self.assertTrue(guard.needs_fix("raspberrypi", "proto", True))
        self.assertTrue(guard.needs_fix("proto", "proto", False))
        self.assertFalse(guard.needs_fix("raspberrypi", "", False))


class CheckTests(unittest.TestCase):
    def run_check(self, hostname, avahi, root=True, code=0):
        with patch.object(guard.os, "name", "posix"), \
                patch.object(guard.os.path, "exists", return_value=True), \
                patch.object(guard.os, "geteuid", create=True, return_value=0 if root else 1000), \
                patch.object(guard.socket, "gethostname", return_value=hostname), \
                patch.object(guard, "_avahi_active", return_value=avahi), \
                patch.object(guard, "wanted_name", return_value="proto"), \
                patch.object(guard, "log_err"), patch("builtins.print"), \
                patch.object(guard.subprocess, "run") as run:
            run.return_value.returncode = code
            run.return_value.stderr = run.return_value.stdout = ""
            return guard.check(), run

    def test_a_healthy_system_is_not_touched(self):
        result, run = self.run_check("proto", True)
        self.assertEqual(result, "ok")
        run.assert_not_called()

    def test_a_wrong_name_is_put_right(self):
        result, run = self.run_check("raspberrypi", True)
        self.assertEqual(result, "fixed")
        self.assertEqual(run.call_args[0][0][-1], "proto")
        self.assertTrue(run.call_args[0][0][1].endswith("setup_hostname.sh"))

    def test_a_stopped_avahi_is_started_again(self):
        self.assertEqual(self.run_check("proto", False)[0], "fixed")

    def test_without_root_it_only_reports(self):
        result, run = self.run_check("raspberrypi", True, root=False)
        self.assertEqual(result, "failed")
        run.assert_not_called()

    def test_a_failing_script_is_reported(self):
        self.assertEqual(self.run_check("raspberrypi", True, code=1)[0], "failed")

    def test_the_page_is_told_the_address(self):
        s = web_app.app.call("GET", "/api/status").get_json()
        self.assertRegex(s["address"], r"^http://[a-z0-9-]+\.local$")


if __name__ == "__main__":
    unittest.main()
