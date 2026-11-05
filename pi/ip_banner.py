"""The face shows the Pi's address for a while after it joins a network.

Phones on Android cannot open http://proto.local, and a phone's own hotspot
does not list the addresses of the devices on it. So when the Pi gets an
address on a network (or the address changes), the face shows it for
config.SHOW_IP_S seconds: open that address in the browser.

Our own hotspot (10.42.0.1) is not shown: proto.local and the "sign in to the
network" screen work there.
"""
import subprocess
import threading
import time

import config
from state import log_err, stop_event

POLL_S = 3
HOTSPOT_ADDRESS = "10.42.0.1"          # hotspot.ADDRESS; not imported, to keep this module light

_lock = threading.Lock()


def show_s():
    return float(getattr(config, "SHOW_IP_S", 30) or 0)


def parse_ips(out):
    """Output of `ip -4 -o addr show scope global` -> [(device, address), ...]."""
    found = []
    for line in out.splitlines():
        f = line.split()
        if "inet" not in f:
            continue
        i = f.index("inet")
        if i + 1 >= len(f) or len(f) < 2:
            continue
        dev, addr = f[1], f[i + 1].split("/")[0]
        if dev == "lo" or addr.startswith("127.") or addr == HOTSPOT_ADDRESS:
            continue
        found.append((dev, addr))
    return found


def pick(found):
    """The address to show: Wi-Fi first, then a cable, then anything else."""
    for prefix in ("wl", "eth", "en", ""):
        for dev, addr in found:
            if dev.startswith(prefix):
                return addr
    return None


def current_ip():
    try:
        r = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return pick(parse_ips(r.stdout)) if r.returncode == 0 else None


def label(ip):
    """What the face shows: the address, with the port when :80 is not served."""
    plain = getattr(config, "WEB_PORT_PLAIN", 80)
    if plain == 80 and config.WEB_PORT != 80:
        return ip
    return f"{ip}:{config.WEB_PORT}"


class Banner:
    """When to show the address. Pure: the caller feeds it the time and the address it sees."""

    def __init__(self, seconds):
        self.seconds = seconds
        self.ip = None
        self.until = 0.0

    def step(self, now, ip):
        if ip != self.ip:
            self.ip = ip
            self.until = now + self.seconds if ip else 0.0

    def show_again(self, now):
        if self.ip:
            self.until = now + self.seconds

    def text(self, now):
        return label(self.ip) if self.ip and now < self.until else None


_banner = Banner(show_s())


def text(now=None):
    """The address to draw on the face right now, or None."""
    with _lock:
        return _banner.text(time.monotonic() if now is None else now)


def show_again():
    """Show the address once more (e.g. from the web page)."""
    with _lock:
        _banner.show_again(time.monotonic())


def _loop():
    while not stop_event.is_set():
        try:
            ip = current_ip()
            with _lock:
                _banner.step(time.monotonic(), ip)
        except Exception as e:                       # never take the face down over this
            log_err(f"ip_banner: {e}")
        stop_event.wait(POLL_S)


def start():
    if show_s() > 0:
        threading.Thread(target=_loop, daemon=True, name="ip-banner").start()
