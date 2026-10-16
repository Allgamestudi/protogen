"""Keep the box reachable at http://proto.local.

On start: if the hostname isn't config.HOSTNAME or avahi isn't running,
run scripts/setup_hostname.sh. A custom name for one device lives in
saves/hostname (setup_hostname.sh writes it).
"""
import os
import re
import socket
import subprocess
import threading

import config
from state import SAVES_DIR, log_err

NAME_FILE = os.path.join(SAVES_DIR, "hostname")
SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "scripts", "setup_hostname.sh")
VALID = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def wanted_name(name_file=NAME_FILE, default=None):
    """The name this device should have, or "" for "leave the system alone"."""
    if default is None:
        default = getattr(config, "HOSTNAME", "proto")
    try:
        with open(name_file, encoding="utf-8") as f:
            saved = f.read().strip().lower()
        if VALID.match(saved):
            return saved
    except OSError:
        pass
    default = (default or "").strip().lower()
    return default if VALID.match(default) else ""


def needs_fix(current, wanted, avahi_active):
    return bool(wanted) and (current != wanted or not avahi_active)


def address():
    """What to type into a browser, for the web page."""
    name = wanted_name() or socket.gethostname()
    return f"http://{name}.local"


def _avahi_active():
    try:
        r = subprocess.run(["systemctl", "is-active", "--quiet", "avahi-daemon"], timeout=10)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def check():
    """One pass. -> "ok", "fixed", "skipped" or "failed"."""
    wanted = wanted_name()
    if not wanted or os.name != "posix" or not os.path.exists(SCRIPT):
        return "skipped"
    if not needs_fix(socket.gethostname(), wanted, _avahi_active()):
        return "ok"
    if os.geteuid() != 0:
        log_err(f"hostname: not '{wanted}' and no rights to fix it (sudo bash scripts/setup_hostname.sh)")
        return "failed"
    try:
        r = subprocess.run(["bash", SCRIPT, wanted], capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        log_err(f"hostname: {e}")
        return "failed"
    if r.returncode != 0:
        log_err("hostname: " + (r.stderr.strip() or r.stdout.strip())[-200:])
        return "failed"
    print(f"hostname: set to {wanted}, the address is http://{wanted}.local")
    return "fixed"


def start():
    threading.Thread(target=check, daemon=True, name="hostname-guard").start()
