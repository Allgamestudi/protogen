"""Own Wi-Fi network for when no known one is around, so a new owner can reach the page.

After power-on the Pi waits for a network it knows. With none (and no cable) after
START_AFTER_S it opens its own network, config.HOTSPOT_SSID; the page is then at
http://10.42.0.1 and http://proto.local. Every address leads to the page, so a phone
that joins shows it as a "sign in to the network" screen.

With nobody on it for RETRY_S, the hotspot goes down for LOOK_S so that
NetworkManager can join a known network that has come into range, and comes back if
none has. Joining a network from the page switches the hotspot off by itself.
"""
import json
import os
import subprocess
import threading
import time

import config
import wifi
from state import SAVES_DIR, log_err, stop_event

CON = wifi.AP_CON
ADDRESS = "10.42.0.1"
PORTAL_DNS = "/etc/NetworkManager/dnsmasq-shared.d/protogen-portal.conf"
START_AFTER_S = 60
RETRY_S = 300
LOOK_S = 45


CONF_FILE = os.path.join(SAVES_DIR, "hotspot.json")    # name and password set on the page


def defaults():
    return {"ssid": getattr(config, "HOTSPOT_SSID", "Protogen"),
            "password": getattr(config, "HOTSPOT_PASSWORD", "protogen")}


def check(name, password):
    """-> "" when the pair can be our network's, else the reason (the page translates it)."""
    reason = wifi.check_input(name, password)
    if reason:
        return reason
    if not 8 <= len(password) <= 63:                    # our own network always has a password
        return "bad_password"
    return ""


def settings():
    """The name and password of our network: the page's, else the defaults from config.py."""
    out = defaults()
    try:
        with open(CONF_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict) and not check(saved.get("ssid"), saved.get("password")):
            out = {"ssid": saved["ssid"], "password": saved["password"]}
    except (OSError, ValueError):
        pass
    return out


def ssid():
    return settings()["ssid"]


def public():
    """What the page shows."""
    s, d = settings(), defaults()
    return {"ssid": s["ssid"], "password": s["password"], "default_ssid": d["ssid"],
            "default_password": d["password"], "custom": s != d, "active": active()}


def _apply():
    """Restart our network with the new name and password if it is up now."""
    if active():
        threading.Thread(target=lambda: (down(), up()), daemon=True, name="hotspot-restart").start()


def save(name, password):
    name = name.strip() if isinstance(name, str) else name
    reason = check(name, password)
    if reason:
        return {"ok": False, "reason": reason}
    data = {"ssid": name, "password": password}
    try:
        if data == defaults():
            if os.path.exists(CONF_FILE):
                os.remove(CONF_FILE)
        else:
            tmp = CONF_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, CONF_FILE)
    except OSError as e:
        log_err(f"hotspot: cannot save: {e}")
        return {"ok": False, "reason": "failed"}
    _apply()
    return {"ok": True, **public()}


def reset():
    try:
        if os.path.exists(CONF_FILE):
            os.remove(CONF_FILE)
    except OSError as e:
        log_err(f"hotspot: cannot reset: {e}")
        return {"ok": False, "reason": "failed"}
    _apply()
    return {"ok": True, **public()}


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
                 "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", settings()["password"])
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


def foreign_aps():
    """Hotspot profiles that are not ours (made by hand or by the OS image).

    NetworkManager brings such a profile up by itself, under its own name and
    password, and the page then thinks the Pi is on an ordinary Wi-Fi network.
    """
    ok, out = _run("nmcli", "-t", "-f", "NAME,TYPE", "connection", "show")
    if not ok:
        return []
    found = []
    for line in out.splitlines():
        f = wifi.split_terse(line)
        if len(f) < 2 or f[0] == CON or f[1] not in ("802-11-wireless", "wifi"):
            continue
        ok, mode = _run("nmcli", "-g", "802-11-wireless.mode", "connection", "show", "id", f[0])
        if ok and mode.strip() == "ap":
            found.append(f[0])
    return found


def remove_foreign_aps():
    for name in foreign_aps():
        ok, _ = _run("nmcli", "connection", "delete", "id", name)
        log_err(f"hotspot: {'removed' if ok else 'could not remove'} a foreign hotspot profile '{name}'")


FOREIGN_SERVICES = ("hostapd", "raspapd")      # hotspots set up by hand or by RaspAP, outside NetworkManager


def _service_on(name):
    """Running now or set to start with the Pi."""
    return (_run("systemctl", "is-active", "--quiet", name, timeout=10)[0]
            or _run("systemctl", "is-enabled", "--quiet", name, timeout=10)[0])


def foreign_services():
    found = [s for s in FOREIGN_SERVICES if _service_on(s)]
    if found and _service_on("dnsmasq"):       # their DHCP/DNS: holds the ports our network needs
        found.append("dnsmasq")
    return found


def stop_foreign_services():
    """Switch off hostapd/RaspAP: they share their own network (e.g. 'proto') and keep wlan0 to themselves."""
    found = foreign_services()
    if not found:
        return []
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None or geteuid() != 0:
        log_err(f"hotspot: {', '.join(found)} running and no rights to stop them")
        return found
    for name in found:
        ok, _ = _run("systemctl", "disable", "--now", name, timeout=60)
        if name != "dnsmasq":
            _run("systemctl", "mask", name, timeout=30)      # so it does not come back after an update
        log_err(f"hotspot: {'switched off' if ok else 'could not switch off'} the service '{name}'")
    return found


def make_managed():
    """Give wlan0 back to NetworkManager when a hand-made setup took it away."""
    ok, out = _run("nmcli", "-t", "-f", "DEVICE,STATE", "device")
    if not ok:
        return False
    for line in out.splitlines():
        f = wifi.split_terse(line)
        if len(f) >= 2 and f[0] == iface() and f[1] == "unmanaged":
            ok, _ = _run("nmcli", "device", "set", f[0], "managed", "yes")
            log_err(f"hotspot: {'gave' if ok else 'could not give'} {f[0]} back to NetworkManager")
            return ok
    return False


def _loop():
    try:
        stop_foreign_services()                      # hostapd/RaspAP: other software sharing its own network
        make_managed()
        remove_foreign_aps()                         # our network is the only one the Pi shares
    except Exception as e:
        log_err(f"hotspot: {e}")
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
