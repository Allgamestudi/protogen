"""Wi-Fi for the web page, via nmcli (NetworkManager). The service runs as root.

status() -> state is "online", "no_internet" or "no_wifi"
scan()   -> networks nearby
connect(ssid, password) runs in the background, see status()["job"]

The password only goes to nmcli, never to logs or back to the page.
"""
import shutil
import socket
import subprocess
import threading
import time

# reaching one of these means "there is internet" (the first is where updates come from)
PROBES = (("github.com", 443), ("1.1.1.1", 443))
PROBE_TIMEOUT = 3.0
CONNECT_TIMEOUT = 40            # seconds nmcli may take to join
AP_CON = "protogen-ap"          # the profile of our own network (hotspot.py)

# Set by the simulator: {"net": "online" | "no_internet" | "no_wifi", "ssid": "..."}.
fake = None

_lock = threading.Lock()
_job = {"state": "idle", "ssid": "", "reason": ""}      # idle / connecting / ok / error
_internet = {"at": 0.0, "ok": False}


def split_terse(line):
    """One line of `nmcli -t`: fields separated by ':', with '\\:' inside a value."""
    out, cur, i = [], "", 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur += line[i + 1]
            i += 2
            continue
        if c == ":":
            out.append(cur)
            cur = ""
        else:
            cur += c
        i += 1
    out.append(cur)
    return out


def _nmcli(*args, timeout=15):
    """-> (ok, stdout, stderr). Never raises."""
    try:
        r = subprocess.run(["nmcli", *args], capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, r.stdout, r.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "", "timeout"
    except OSError as e:
        return False, "", str(e)


def available():
    return fake is not None or shutil.which("nmcli") is not None


def has_internet(max_age=5.0):
    now = time.monotonic()
    with _lock:
        if now - _internet["at"] < max_age:
            return _internet["ok"]
    ok = False
    for host, port in PROBES:
        try:
            socket.create_connection((host, port), timeout=PROBE_TIMEOUT).close()
            ok = True
            break
        except OSError:
            pass
    with _lock:
        _internet["at"], _internet["ok"] = time.monotonic(), ok
    return ok


def parse_devices(text):
    """Output of `nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device` -> (link, ssid)."""
    link, ssid = "none", None
    for line in text.splitlines():
        f = split_terse(line)
        if len(f) < 4 or f[2] != "connected":
            continue
        if f[1] == "wifi":
            return "wifi", f[3]
        if f[1] == "ethernet":
            link = "ethernet"
    return link, ssid


def status(check_internet=True):
    with _lock:
        job = dict(_job)
    if fake is not None:
        net = fake.get("net", "online")
        link = "none" if net == "no_wifi" else "wifi"
        return {"available": True, "link": link, "ssid": fake.get("ssid") if link == "wifi" else None,
                "internet": net == "online", "state": net, "job": job}
    if not available():
        # no NetworkManager: only the internet itself can be checked
        online = has_internet() if check_internet else False
        return {"available": False, "link": "unknown", "ssid": None, "internet": online,
                "state": "online" if online else "no_internet", "job": job}
    ok, out, _ = _nmcli("-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device")
    link, ssid = parse_devices(out) if ok else ("unknown", None)
    if link == "wifi" and ssid == AP_CON:              # we are the network: nobody to reach the internet through
        import config
        return {"available": True, "link": "hotspot", "ssid": getattr(config, "HOTSPOT_SSID", "Protogen"),
                "internet": False, "state": "no_wifi", "job": job}
    if link == "none":
        return {"available": True, "link": link, "ssid": None, "internet": False,
                "state": "no_wifi", "job": job}
    online = has_internet() if check_internet else False
    return {"available": True, "link": link, "ssid": ssid, "internet": online,
            "state": "online" if online else "no_internet", "job": job}


def parse_scan(text):
    """Output of `nmcli -t -f IN-USE,SSID,SIGNAL,SECURITY device wifi list`."""
    best = {}
    for line in text.splitlines():
        f = split_terse(line)
        if len(f) < 4 or not f[1]:
            continue                                    # hidden networks have no name
        try:
            signal = int(f[2])
        except ValueError:
            signal = 0
        net = {"ssid": f[1], "signal": signal, "secure": f[3] not in ("", "--"), "active": f[0] == "*"}
        old = best.get(f[1])
        if old is None or signal > old["signal"] or net["active"]:
            net["active"] = net["active"] or bool(old and old["active"])
            best[f[1]] = net
    return sorted(best.values(), key=lambda n: (not n["active"], -n["signal"]))


def scan():
    if fake is not None:
        here = fake.get("ssid") if fake.get("net") != "no_wifi" else None
        nets = [{"ssid": "Home", "signal": 82, "secure": True, "active": False},
                {"ssid": "Phone hotspot", "signal": 64, "secure": True, "active": False},
                {"ssid": "Cafe free", "signal": 41, "secure": False, "active": False}]
        for n in nets:
            n["active"] = n["ssid"] == here
        return nets
    ok, out, _ = _nmcli("-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list",
                        "--rescan", "auto", timeout=25)
    return parse_scan(out) if ok else []


def check_input(ssid, password):
    """-> "" when the name and the password can be a Wi-Fi network's, else the reason."""
    if not isinstance(ssid, str) or not isinstance(password, str):
        return "bad_input"
    if not ssid or len(ssid.encode("utf-8")) > 32 or any(ord(c) < 32 for c in ssid):
        return "bad_ssid"
    if password and not 8 <= len(password) <= 64:
        return "bad_password"                           # WPA: 8..63 characters (or 64 hex digits)
    if any(ord(c) < 32 for c in password):
        return "bad_password"
    return ""


def failure_reason(stderr):
    """nmcli's complaint -> a short reason the page can translate."""
    text = stderr.lower()
    if "secrets were required" in text or "password" in text or "802-11-wireless-security" in text:
        return "wrong_password"
    if "no network with ssid" in text or "not found" in text:
        return "not_found"
    if "timeout" in text:
        return "timeout"
    return "failed"


def _known_profiles():
    ok, out, _ = _nmcli("-t", "-f", "NAME", "connection", "show")
    return {split_terse(line)[0] for line in out.splitlines()} if ok else set()


def _join(ssid, password, before):
    if fake is not None:
        time.sleep(1.5)
        nets = {n["ssid"]: n for n in scan()}
        reason = ("not_found" if ssid not in nets
                  else "wrong_password" if nets[ssid]["secure"] and not password else "")
        if not reason:
            fake["net"], fake["ssid"] = "online", ssid
    else:
        known = _known_profiles()
        args = ["--wait", str(CONNECT_TIMEOUT), "device", "wifi", "connect", ssid]
        if password:
            args += ["password", password]
        ok, _, err = _nmcli(*args, timeout=CONNECT_TIMEOUT + 10)
        reason = "" if ok else failure_reason(err)
        if reason:
            # do not leave a profile that cannot connect, and go back to where we were
            if ssid not in known:
                _nmcli("connection", "delete", "id", ssid)
            if before and before != ssid:
                _nmcli("connection", "up", "id", before, timeout=CONNECT_TIMEOUT)
    with _lock:
        _internet["at"] = 0.0                           # the answer about internet is stale now
        _job.update(state="error" if reason else "ok", reason=reason)


def saved():
    """Networks the Pi remembers and joins by itself: [{"ssid", "active"}, ...]."""
    if fake is not None:
        here = fake.get("ssid") if fake.get("net") != "no_wifi" else None
        names = fake.setdefault("saved", ["Home"])
        return [{"ssid": n, "active": n == here} for n in names]
    ok, out, _ = _nmcli("-t", "-f", "NAME,TYPE,ACTIVE", "connection", "show")
    nets = []
    for line in out.splitlines() if ok else []:
        f = split_terse(line)
        if len(f) >= 3 and f[1] in ("802-11-wireless", "wifi") and f[0] != AP_CON:
            nets.append({"ssid": f[0], "active": f[2] == "yes"})
    return nets


def add(ssid, password=""):
    """Remember a network without joining it now (it is joined when it is in range)."""
    reason = check_input(ssid, password)
    if reason:
        return {"ok": False, "reason": reason}
    if fake is not None:
        names = fake.setdefault("saved", ["Home"])
        if ssid not in names:
            names.append(ssid)
        return {"ok": True}
    if not available():
        return {"ok": False, "reason": "unavailable"}
    if ssid in _known_profiles():
        _nmcli("connection", "delete", "id", ssid)
    args = ["connection", "add", "type", "wifi", "con-name", ssid, "ifname", "*", "ssid", ssid,
            "connection.autoconnect", "yes"]
    if password:
        args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
    ok, _, err = _nmcli(*args)
    return {"ok": True} if ok else {"ok": False, "reason": "failed"}


def forget(ssid):
    """Drop a remembered network. The one in use stays: the page would lose the Pi."""
    for net in saved():
        if net["ssid"] == ssid:
            if net["active"]:
                return {"ok": False, "reason": "active"}
            if fake is not None:
                fake["saved"].remove(ssid)
                return {"ok": True}
            ok, _, _ = _nmcli("connection", "delete", "id", ssid)
            return {"ok": ok} if ok else {"ok": False, "reason": "failed"}
    return {"ok": False, "reason": "not_found"}


def connect(ssid, password=""):
    """Start joining a network. -> {"ok": True} or {"ok": False, "reason": ...}."""
    reason = check_input(ssid, password)
    if reason:
        return {"ok": False, "reason": reason}
    if not available():
        return {"ok": False, "reason": "unavailable"}
    before = status(check_internet=False).get("ssid")
    with _lock:
        if _job["state"] == "connecting":
            return {"ok": False, "reason": "busy"}
        _job.update(state="connecting", ssid=ssid, reason="")
    threading.Thread(target=_join, args=(ssid, password, before), daemon=True, name="wifi-join").start()
    return {"ok": True}
