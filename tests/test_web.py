"""The web interface: the settings catalogue, the Flask stand-in, the routes.

Run from the project root: python -B -m unittest discover -s tests -v
"""
import io
from pathlib import Path
import re
import sys
from types import ModuleType
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pi"))

# the routes are tested without Flask and without hardware
sys.modules["flask"] = None
if "serial_proto" not in sys.modules:
    fake = ModuleType("serial_proto")
    fake.send_command = lambda command: None
    sys.modules["serial_proto"] = fake
try:
    import serial                                                   # noqa: F401
except ImportError:
    stub = ModuleType("serial")
    stub.SerialException = stub.SerialTimeoutException = OSError
    sys.modules["serial"] = stub
with patch("os.makedirs"):
    import config                                                   # noqa: E402,F401
import miniflask                                                    # noqa: E402
import state                                                        # noqa: E402
import ui_schema                                                    # noqa: E402
import web_app                                                      # noqa: E402

PAGE = (ROOT / "pi" / "webui.html").read_text(encoding="utf-8")


class SchemaTests(unittest.TestCase):
    def items(self):
        return [it for g in ui_schema.GROUPS for it in g["items"]]

    def test_every_item_is_a_real_setting_once(self):
        keys = [it["key"] for it in self.items()]
        self.assertEqual(len(keys), len(set(keys)))
        for key in keys:
            self.assertIn(key, state.DEFAULT_SETTINGS)

    def test_both_languages_everywhere(self):
        for g in ui_schema.GROUPS:
            for field in ("title", "about"):
                for lang in ("en", "ru"):
                    self.assertTrue(g[field][lang].strip(), (g["id"], field, lang))
            for it in g["items"]:
                for field in ("label", "help"):
                    for lang in ("en", "ru"):
                        self.assertTrue(it[field][lang].strip(), (it["key"], field, lang))
                self.assertGreater(len(it["help"]["ru"]), 20, it["key"])

    def test_defaults_fit_the_controls(self):
        for it in self.items():
            default = state.DEFAULT_SETTINGS[it["key"]]
            if it["kind"] == "slider":
                self.assertLessEqual(it["min"], float(default), it["key"])
                self.assertGreaterEqual(it["max"], float(default), it["key"])
                self.assertGreater(it["step"], 0)
            elif it["kind"] == "choice" and it["options"] != "@emotions":
                self.assertIn(default, it["options"], it["key"])

    def test_old_page_still_gets_its_format(self):
        self.assertTrue(ui_schema.legacy())


class PageTests(unittest.TestCase):
    def table(self, lang):
        body = PAGE[PAGE.index(lang + ":{"):]
        return set(re.findall(r'(?:[{,]|\n )([A-Za-z_][A-Za-z0-9_]*):"', body[:body.index("}")]))

    def test_translations_match(self):
        en, ru = self.table("en"), self.table("ru")
        self.assertGreater(len(en), 100)
        self.assertEqual(en ^ ru, set())

    def test_every_text_on_the_page_is_translated(self):
        en = self.table("en")
        used = set(re.findall(r'data-tp?="(\w+)"', PAGE)) | set(re.findall(r'\bt\("(\w+)"\)', PAGE))
        self.assertEqual(used - en, set())


class RouteTests(unittest.TestCase):
    def call(self, method, path, body=None):
        return web_app.app.call(method, path, json_body=body)

    def test_pages(self):
        r = self.call("GET", "/")
        self.assertEqual(r.status, 200)
        self.assertIn(b"/api/ui/schema", r.body)
        self.assertIn(b"<html", self.call("GET", "/classic").body)

    def test_schema_and_status(self):
        j = self.call("GET", "/api/ui/schema").get_json()
        self.assertEqual(set(j), {"groups", "defaults", "simulated"})
        s = self.call("GET", "/api/status").get_json()
        self.assertIn("settings", s)
        self.assertIn("simulated", s)

    def test_set_and_settings(self):
        self.assertEqual(self.call("POST", "/api/set", {"emotion": "happy"}).status, 200)
        self.assertEqual(state.state["emotion"], "happy")
        before = state.state["settings"]["liveliness"]
        with patch.object(web_app, "save_settings", return_value=True):      # never touch saves/
            self.call("POST", "/api/settings", {"liveliness": 1.5})
        self.assertEqual(state.state["settings"]["liveliness"], 1.5)
        state.state["settings"]["liveliness"] = before

    def test_preview(self):
        state.preview["frame"], state.preview["seq"] = None, 0
        self.assertEqual(self.call("GET", "/api/preview.png").status, 204)
        frame = np.zeros((32, 128, 3), np.uint8)
        frame[4, 7] = (255, 10, 20)
        state.preview["frame"], state.preview["seq"] = frame, 12345
        r = self.call("GET", "/api/preview.png")
        self.assertEqual(r.mimetype, "image/png")
        back = np.asarray(Image.open(io.BytesIO(r.body)).convert("RGB"))
        self.assertTrue(np.array_equal(back, frame))

    def test_simulator_cannot_touch_the_machine(self):
        with patch.object(web_app, "SIMULATED", True), patch("subprocess.Popen") as popen, \
                patch("subprocess.run") as run:
            for path in ("/api/system/restart", "/api/system/reboot", "/api/system/update"):
                self.assertFalse(self.call("POST", path, {}).get_json()["ok"], path)
            popen.assert_not_called()
            run.assert_not_called()

    def test_unknown_path_and_method(self):
        self.assertEqual(self.call("GET", "/nope").status, 404)
        self.assertEqual(self.call("GET", "/api/set").status, 405)


class MiniFlaskTests(unittest.TestCase):
    def test_multipart_upload(self):
        app = miniflask.Flask()

        @app.route("/up", methods=["POST"])
        def up():
            f = miniflask.request.files["file"]
            return miniflask.jsonify({"name": f.filename, "size": len(f.read()),
                                      "label": miniflask.request.form.get("label")})

        b = "XBOUND"
        body = (f'--{b}\r\nContent-Disposition: form-data; name="label"\r\n\r\nпривет\r\n'
                f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="a.bin"\r\n'
                f'Content-Type: application/octet-stream\r\n\r\n').encode() + bytes(range(256)) + \
            f"\r\n--{b}--\r\n".encode()
        r = app.call("POST", "/up", body=body, content_type=f"multipart/form-data; boundary={b}")
        self.assertEqual(r.get_json(), {"name": "a.bin", "size": 256, "label": "привет"})

    def test_a_broken_view_answers_500(self):
        app = miniflask.Flask()
        app.route("/x")(lambda: 1 / 0)
        with patch("traceback.print_exc"):
            self.assertEqual(app.call("GET", "/x").status, 500)

    def test_wsgi(self):
        app = miniflask.Flask()
        app.route("/x")(lambda: ("hi", 200))
        seen = {}
        out = app({"REQUEST_METHOD": "GET", "PATH_INFO": "/x", "wsgi.input": io.BytesIO()},
                  lambda status, headers: seen.update(status=status, headers=dict(headers)))
        self.assertEqual((out, seen["status"]), ([b"hi"], "200 OK"))


if __name__ == "__main__":
    unittest.main()
