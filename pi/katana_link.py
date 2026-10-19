"""Follow the katana prop: ask it about its blade about once a second.

The katana also reports to /api/katana by itself, but it often cannot find
proto.local; the Pi resolves katana.local without trouble, so it asks too.
Both end in web_app.katana_apply(), which only acts on changes.
"""
import json
import socket
import threading
import time
import urllib.request

import config
from state import log_err, stop_event

POLL_S = 0.8
LOOK_EVERY_S = 15


def parse(st):
    """The katana's /api/state -> (blade lit and syncing, [r, g, b] or None)."""
    on = bool(st.get("on")) and bool(st.get("protoSync", 1))
    colour = None
    c = st.get("colour")
    if isinstance(c, str) and len(c) == 7 and c.startswith("#"):
        try:
            v = int(c[1:], 16)
            colour = [(v >> 16) & 255, (v >> 8) & 255, v & 255]
        except ValueError:
            pass
    return on, colour


def _loop():
    import web_app                                   # late: web_app is still loading when this module is
    ip, looked = None, 0.0
    while not stop_event.wait(POLL_S):
        if not getattr(config, "KATANA_SYNC", True):
            continue
        if ip is None:
            if time.monotonic() - looked < LOOK_EVERY_S:
                continue
            looked = time.monotonic()
            try:
                ip = socket.gethostbyname(getattr(config, "KATANA_HOST", "katana.local"))
            except OSError:
                continue
        try:
            with urllib.request.urlopen(f"http://{ip}/api/state", timeout=2) as r:
                st = json.loads(r.read())
        except (OSError, ValueError):
            ip = None                                # gone or moved: look it up again
            web_app.katana_apply(False)
            continue
        try:
            web_app.katana_apply(*parse(st))
        except Exception as e:                      # never take the service down
            log_err(f"katana: {e}")


def start():
    threading.Thread(target=_loop, daemon=True, name="katana-link").start()
