"""Flask web app - state JSON API + command relay + image upload + settings."""
import os
import time
import numpy as np
try:
    from flask import Flask, jsonify, request, send_file, Response
except ImportError:                      # simulator and tests on a PC without Flask
    from miniflask import Flask, jsonify, request, send_file, Response

import config
import emotions
from state import (
    state, lock, uploaded_image, uploaded_gif_frames,
    DEFAULT_SETTINGS, update_setting, save_settings, reset_settings,
    SETTINGS_FILE, mark_ui_dirty, log_err,
)
import state as shared
import image_convert
import ui_schema
import hostname_guard
import updater
import wifi
from serial_proto import send_command
import ears
import ears_effect


app = Flask(__name__)


WRITABLE = {
    "mode", "emotion", "effect", "color",
    "brightness", "rotate_180", "mirror",
    "gradient_colors", "gradient_angle",
    "text_content", "text_color",
    "party", "wink_request",
}


# The pages are plain files next to this one: webui.html (current) and
# webui_classic.html (the previous page, still reachable at /classic).
_HERE = os.path.dirname(os.path.abspath(__file__))


def _page(name):
    try:
        with open(os.path.join(_HERE, name), encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        return f"<h1>{name} is missing</h1><pre>{e}</pre>"


# Set by the simulator: nothing may restart services or reboot the machine then.
SIMULATED = False

@app.route("/")
def index():
    return Response(_page("webui.html"), mimetype="text/html")


@app.route("/classic")
def index_classic():
    return Response(_page("webui_classic.html"), mimetype="text/html")


@app.route("/api/ui/schema")
def api_ui_schema():
    """Settings with names and explanations in every language, and their defaults."""
    return jsonify({"groups": ui_schema.GROUPS, "defaults": dict(DEFAULT_SETTINGS),
                    "simulated": SIMULATED})


_preview_cache = {"seq": -1, "png": b""}


@app.route("/api/preview.png")
def api_preview():
    """What the panels show right now, as a small PNG (one pixel per LED)."""
    import io
    from PIL import Image
    frame, seq = shared.preview["frame"], shared.preview["seq"]
    if frame is None:
        return ("", 204)
    if seq != _preview_cache["seq"]:
        buf = io.BytesIO()
        Image.fromarray(frame).save(buf, "PNG", compress_level=1)
        _preview_cache["seq"], _preview_cache["png"] = seq, buf.getvalue()
    r = Response(_preview_cache["png"], mimetype="image/png")
    r.headers["Cache-Control"] = "no-store"
    return r


@app.route("/api/ears/preview")
def api_ears_preview():
    """Colours of the ear LEDs right now."""
    return jsonify({"left": [list(c) for c in ears.left], "right": [list(c) for c in ears.right]})


@app.route("/api/emotions")
def api_emotions():
    return jsonify(emotions.list_emotions())


@app.route("/api/emotions/custom", methods=["POST"])
def api_emotions_custom_add():
    """Add an emotion from uploaded pictures of its parts."""
    if not hasattr(emotions, "add_custom"):
        return jsonify({"ok": False, "error": "artwork faces are switched off (EMOTION_ART)"}), 400
    masks = {}
    try:
        for part in ("eye_open", "eye_closed", "eye_blink", "mouth_closed", "mouth_open"):
            f = request.files.get(part)
            if f:
                masks[part] = image_convert.convert_mask(f.read(), config.W, config.H)
        eid = emotions.add_custom(request.form.get("label", ""), request.form.get("emoji", ""), masks)
    except (image_convert.ConvertError, ValueError) as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        log_err(f"custom emotion: {e}")
        return jsonify({"ok": False, "error": f"failed: {e}"}), 500
    return jsonify({"ok": True, "id": eid})


@app.route("/api/emotions/custom/delete", methods=["POST"])
def api_emotions_custom_delete():
    eid = (request.get_json(silent=True) or {}).get("id", "")
    if not hasattr(emotions, "remove_custom") or not emotions.remove_custom(eid):
        return jsonify({"ok": False, "error": "no such custom emotion"}), 404
    with lock:
        if state.get("emotion") == eid:
            state["emotion"] = config.DEFAULTS["emotion"]
        state["emotion_colors"].pop(eid, None)
    mark_ui_dirty()
    return jsonify({"ok": True})


@app.route("/api/system/restart", methods=["POST"])
def api_system_restart():
    """Restart just the protogen service (~3 sec downtime)."""
    if SIMULATED:
        return jsonify({"ok": False, "error": "simulator: nothing to restart"})
    import subprocess, threading
    def _later():
        time.sleep(0.5)
        subprocess.Popen(["sudo", "systemctl", "restart", "protogen"])
    threading.Thread(target=_later, daemon=True).start()
    return jsonify({"ok": True, "action": "service restart in 0.5s"})


@app.route("/api/system/reboot", methods=["POST"])
def api_system_reboot():
    """Full Pi reboot (~30 sec downtime)."""
    if SIMULATED:
        return jsonify({"ok": False, "error": "simulator: nothing to reboot"})
    import subprocess, threading
    def _later():
        time.sleep(1.0)
        subprocess.Popen(["sudo", "/sbin/reboot"])
    threading.Thread(target=_later, daemon=True).start()
    return jsonify({"ok": True, "action": "reboot in 1s"})


@app.route("/api/system/version")
def api_system_version():
    return jsonify(updater.version())


NET_ERRORS = {"no_wifi": "Wi-Fi is not connected", "no_internet": "no access to the internet"}


def _no_network():
    """None when the update server can be reached, else the answer for the page."""
    net = wifi.status()["state"]
    if net == "online":
        return None
    return {"ok": False, "reason": net, "error": NET_ERRORS.get(net, net)}


@app.route("/api/system/update/check", methods=["POST"])
def api_system_update_check():
    return jsonify(_no_network() or updater.check())


@app.route("/api/wifi")
def api_wifi():
    return jsonify(wifi.status())


@app.route("/api/wifi/scan")
def api_wifi_scan():
    return jsonify({"networks": wifi.scan()})


@app.route("/api/wifi/connect", methods=["POST"])
def api_wifi_connect():
    d = request.get_json(silent=True) or {}
    return jsonify(wifi.connect(d.get("ssid"), d.get("password") or ""))


@app.route("/api/wifi/saved")
def api_wifi_saved():
    return jsonify({"networks": wifi.saved()})


@app.route("/api/wifi/add", methods=["POST"])
def api_wifi_add():
    d = request.get_json(silent=True) or {}
    return jsonify(wifi.add(d.get("ssid"), d.get("password") or ""))


@app.route("/api/wifi/forget", methods=["POST"])
def api_wifi_forget():
    d = request.get_json(silent=True) or {}
    return jsonify(wifi.forget(d.get("ssid") or ""))


@app.route("/api/system/update/setup", methods=["POST"])
def api_system_update_setup():
    if SIMULATED:
        return jsonify({"ok": False, "error": "simulator: updates are switched off"})
    d = request.get_json(silent=True) or {}
    return jsonify(_no_network() or updater.setup((d.get("url") or "").strip(), (d.get("token") or "").strip()))


@app.route("/api/system/update/status")
def api_system_update_status():
    return jsonify(updater.status())


@app.route("/api/system/update", methods=["POST"])
def api_system_update():
    if SIMULATED:
        return jsonify({"ok": False, "error": "simulator: updates are switched off"})
    d = request.get_json(silent=True) or {}
    return jsonify(_no_network() or updater.start(force=bool(d.get("force"))))


@app.route("/api/system/launch_doom", methods=["POST"])
def api_system_launch_doom():
    if SIMULATED:
        return jsonify({"ok": False, "error": "simulator"})
    import os
    import subprocess
    import threading

    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    launcher = os.path.join(project_dir, "scripts", "run_doom.sh")

    if not os.path.exists(launcher):
        return jsonify({
            "ok": False,
            "error": f"{launcher} не найден"
        }), 500

    doom_candidates = [
        "/home/wild/doomgeneric/doomgeneric/doomgeneric_matrix",
        os.path.expanduser("~/doomgeneric/doomgeneric/doomgeneric_matrix"),
    ]

    if not any(os.path.exists(x) for x in doom_candidates):
        return jsonify({
            "ok": False,
            "error": "doomgeneric_matrix не найден"
        }), 500

    def _launch():
        time.sleep(0.5)

        subprocess.Popen(
            [
                "systemd-run",
                "--no-block",
                "--collect",
                "--unit=protogen-doom",
                "/bin/bash",
                launcher,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    threading.Thread(target=_launch, daemon=True).start()

    return jsonify({
        "ok": True,
        "action": "DOOM запускается"
    })


@app.route("/api/status")
def api_status():
    with lock:
        s = dict(state)
        if s["last_eq_ms"]:
            s["age_ms"] = int(time.time() * 1000) - s["last_eq_ms"]
        else:
            s["age_ms"] = None
        s["bars"] = [int(x) for x in s["bars"]]
        s["errors"] = list(s["errors"])
        s["settings"] = dict(s["settings"])
    s["simulated"] = SIMULATED
    s["address"] = hostname_guard.address()
    return jsonify(s)


@app.route("/api/set", methods=["POST"])
def api_set():
    d = request.get_json(silent=True) or {}
    with lock:
        # Switching emotion -> recall its remembered color
        # (unless a color is set in the same request).
        if "emotion" in d and "color" not in d:
            saved = state["emotion_colors"].get(d["emotion"])
            if isinstance(saved, list) and len(saved) == 3:
                state["color"] = list(saved)
        for k, v in d.items():
            if k in WRITABLE:
                state[k] = v
        # Setting a color -> remember it for the current emotion.
        if "color" in d:
            state["emotion_colors"][state["emotion"]] = list(state["color"])
    mark_ui_dirty()
    return jsonify({"ok": True})


@app.route("/api/dino/jump", methods=["POST"])
def api_dino_jump():
    with lock:
        state["dino_jump"] = True
    return jsonify({"ok": True})


@app.route("/api/cmd", methods=["POST"])
def api_cmd():
    d = request.get_json(silent=True) or {}
    c = str(d.get("cmd", "")).upper()
    v = d.get("value", 0)
    if c in ("C", "R"):
        send_command(c)
    elif c in ("M", "G", "A", "S"):
        try: vi = int(v)
        except (TypeError, ValueError): vi = 0
        send_command(f"{c}{vi}")
    return jsonify({"ok": True})


@app.route("/api/upload", methods=["POST"])
def api_upload():
    from PIL import Image
    # Force registration of common format plugins - Image.init() alone is
    # sometimes flaky inside a venv where lazy-import didn't fire.
    try:
        from PIL import GifImagePlugin, PngImagePlugin, JpegImagePlugin, BmpImagePlugin, WebPImagePlugin  # noqa: F401
    except Exception:
        pass
    Image.init()
    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "error": "no file"}), 400
    try:
        frames, info = image_convert.convert_upload(f.read(), config.W, config.H)
    except image_convert.ConvertError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"conversion failed: {e}"}), 400

    with lock:
        # Under the lock: the renderer takes its copy of this list under the
        # same lock, so it sees the old animation or the new one, never a mix.
        uploaded_gif_frames[:] = frames if info["animated"] else []
        uploaded_image[:] = frames[0][0]
        state["has_image"] = True
        state["mode"] = "image"
    try:
        Image.fromarray(frames[0][0]).resize((256, 128), Image.NEAREST).save(
            os.path.join(config.UPLOAD_DIR, "last.png"))
    except Exception as e:
        log_err(f"upload preview: {e}")
    return jsonify({"ok": True, **info})


@app.route("/api/image")
def api_image():
    path = os.path.join(config.UPLOAD_DIR, "last.png")
    if not os.path.exists(path):
        return ("", 404)
    return send_file(path, mimetype="image/png", max_age=0)


# ---------- settings ----------
@app.route("/api/settings/schema")
def api_settings_schema():
    return jsonify(ui_schema.legacy())             # for the old page


@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    with lock:
        return jsonify(dict(state["settings"]))


@app.route("/api/settings", methods=["POST"])
def api_settings_post():
    d = request.get_json(silent=True) or {}
    ok = []
    rejected = []
    for k, v in d.items():
        if update_setting(k, v):
            ok.append(k)
        else:
            rejected.append(k)
    saved = save_settings() if ok else True
    return jsonify({"ok": True, "updated": ok, "rejected": rejected,
                    "saved": saved, "file": SETTINGS_FILE})


@app.route("/api/settings/reset", methods=["POST"])
def api_settings_reset():
    reset_settings()
    return jsonify({"ok": True})


# ear rings
@app.route("/api/ears/set", methods=["POST"])
def api_ears_set():
    """Set ear ring colours and push immediately.

    JSON body:
      {"all": [r, g, b]}                - every LED on both ears, single colour
      {"left":  [[r,g,b], ...], "right": [[r,g,b], ...]}    - per-LED arrays
      {"clear": true}                   - all off
    """
    d = request.get_json(silent=True) or {}
    if d.get("clear"):
        ears.clear()
    elif "all" in d:
        c = d["all"]
        ears.set_all((int(c[0]), int(c[1]), int(c[2])))
    else:
        if "left" in d:
            ears.set_left([(int(c[0]), int(c[1]), int(c[2])) for c in d["left"]])
        if "right" in d:
            ears.set_right([(int(c[0]), int(c[1]), int(c[2])) for c in d["right"]])
    ears.push()
    return jsonify({"ok": True})


@app.route("/api/ears/test", methods=["POST"])
def api_ears_test():
    """Quick visual test - red left, blue right, then a green circle, then off."""
    import time as _t
    # Red left, blue right
    ears.set_left([(255, 0, 0)] * ears.EAR_COUNT)
    ears.set_right([(0, 0, 255)] * ears.EAR_COUNT)
    ears.push()
    _t.sleep(1.0)
    # Green chase
    for i in range(ears.EAR_COUNT):
        l = [(0, 0, 0)] * ears.EAR_COUNT
        r = [(0, 0, 0)] * ears.EAR_COUNT
        l[i] = (0, 255, 0)
        r[i] = (0, 255, 0)
        ears.set_left(l); ears.set_right(r)
        ears.push()
        _t.sleep(0.07)
    ears.clear(); ears.push()
    return jsonify({"ok": True})


@app.route("/api/ears/effect", methods=["GET", "POST"])
def api_ears_effect():
    """Get or set ear effect, picker colour, brightness."""
    if request.method == "GET":
        return jsonify(ears_effect.get_state())
    d = request.get_json(silent=True) or {}
    if "effect" in d:
        ears_effect.set_effect(d["effect"])
    if "color" in d:
        c = d["color"]
        ears_effect.set_color((int(c[0]), int(c[1]), int(c[2])))
    if "brightness" in d:
        ears_effect.set_brightness(float(d["brightness"]))
    return jsonify({"ok": True, "state": ears_effect.get_state()})


def run():
    from wsgiref.simple_server import make_server, WSGIServer, WSGIRequestHandler
    from socketserver import ThreadingMixIn
    import socket
    # Dual-stack server: listens on both IPv4 and IPv6 so mDNS clients
    # (ender.local -> fe80::...) and direct-IPv4 clients both work.
    # One thread per request: the page polls the status and the preview several
    # times a second, and a slow request (an upload, the ear test) must not
    # freeze the rest. All shared data is guarded by state.lock already.
    class DualStackServer(ThreadingMixIn, WSGIServer):
        daemon_threads = True
        address_family = socket.AF_INET6
        def server_bind(self):
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            return super().server_bind()
    print(f"LED Control → http://[::]:{config.WEB_PORT} (dual-stack)")
    print(f"Startup: mode=emotion, emotion=angry, mirror={config.MIRROR_LR}")
    # Also on port 80, so that the address is simply http://proto.local
    # (needs root, which the service has; skipped silently otherwise).
    extra = getattr(config, "WEB_PORT_PLAIN", 80)
    if extra and extra != config.WEB_PORT:
        try:
            plain = make_server("::", extra, app, server_class=DualStackServer)
            import threading
            threading.Thread(target=plain.serve_forever, daemon=True).start()
            print(f"LED Control → also on port {extra}")
        except OSError as e:
            print(f"port {extra} not available ({e}); use :{config.WEB_PORT}")
    make_server("::", config.WEB_PORT, app, server_class=DualStackServer).serve_forever()
