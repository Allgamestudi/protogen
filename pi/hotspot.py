"""Own Wi-Fi network for when no known one is around, so a new owner can reach the page.

After power-on the Pi waits for a network it knows. With none (and no cable) after
START_AFTER_S it opens its own network, config.HOTSPOT_SSID; the page is then at
http://10.42.0.1 and http://proto.local. Every address leads to the page, so a phone
that joins shows it as a "sign in to the network" screen.

With nobody on it for RETRY_S, the hotspot goes down for LOOK_S so that
NetworkManager can join a known network that has come into range, and comes back if
none has. Joining a network from the page switches the hotspot off by itself.
"""
import os
import subprocess
import threading
import time

import config
import wifi
from state import log_err, stop_event

CON = wifi.AP_CON
ADDRESS = "10.42.0.1"
PORTAL_DNS = "/etc/NetworkManager/dnsmasq-shared.d/protogen-portal.conf"
START_AFTER_S = 60
RETRY_S = 300
LOOK_S = 45


def ssid():
    return getattr(config, "HOTSPOT_SSID", "Protogen")


def iface():
    return getattr(config, "HOTSPOT_IFACE", "wlan0")


def _run(*cmd, timeout=30):
    try:
        r = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, r.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False, ""


def active():
    ok, out = _run("nmcli", "-t", "-f", "NAME", "connection", "show", "--active")
    return ok and CON in [wifi.split_terse(line)[0] for line in out.splitlines()]


def clients():
    """Phones on the hotspot, counted from the neighbour table."""
    ok, out = _run("ip", "neigh", "show", "dev", iface())
    return sum(1 for line in out.splitlines() if line.strip() and "FAILED" not in line) if ok else 0


def up():
    try:
        os.makedirs(os.path.dirname(PORTAL_DNS), exist_ok=True)
        with open(PORTAL_DNS, "w") as f:
            f.write(f"address=/#/{ADDRESS}\n")         # every name points at the Pi: the page opens by itself
    except OSError as e:
        log_err(f"hotspot: no portal DNS: {e}")
    _run("nmcli", "connection", "delete", "id", CON)
    ok, _ = _run("nmcli", "connection", "add", "type", "wifi", "ifname", iface(), "con-name", CON,
                 "autoconnect", "no", "ssid", ssid(), "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg",
                 "ipv4.method", "shared", "ipv4.addresses", f"{ADDRESS}/24",
                 "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", getattr(config, "HOTSPOT_PASSWORD", "protogen"))
    if ok:
        ok, _ = _run("nmcli", "connection", "up", "id", CON, timeout=40)
    if not ok:
        log_err("hotspot: could not start")
    return ok


def down():
    _run("nmcli", "connection", "down", "id", CON)


class Keeper:
    """When to open and close the hotspot. Pure: the caller feeds it what it sees."""

    def __init__(self):
        self.offline_since = None
        self.ap_since = None
        self.started = False

    def step(self, now, link, connecting, ap_on, phones):
        """-> "up", "down" or None."""
        if connecting:                                  # the page is joining a network: leave it alone
            return None
        if ap_on:
            self.offline_since = None
            if self.ap_since is None or phones:
                self.ap_since = now
            elif now - self.ap_since >= RETRY_S:
                self.ap_since = None
                return "down"
            return None
        self.ap_since = None
        if link in ("wifi", "ethernet"):
            self.offline_since = None
            self.started = True
            return None
        if self.offline_since is None:
            self.offline_since = now
        wait = LOOK_S if self.started else START_AFTER_S
        if now - self.offline_since >= wait:
            self.offline_since = None
            self.started = True
            self.ap_since = now
            return "up"
        return None


def _loop():
    keeper = Keeper()
    while not stop_event.wait(5):
        try:
            st = wifi.status(check_internet=False)
            on = active()
            action = keeper.step(time.monotonic(), st["link"], st["job"]["state"] == "connecting", on,
                                 clients() if on else 0)
            if action == "up":
                up()
            elif action == "down":
                down()
        except Exception as e:                          # never take the service down
            log_err(f"hotspot: {e}")


def start():
    if not getattr(config, "HOTSPOT", True) or os.name != "posix" or wifi.fake is not None or not wifi.available():
        return
    threading.Thread(target=_loop, daemon=True, name="hotspot").start()
