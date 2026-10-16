"""Update from git for the web page: version(), check(), start(), status().

The actual update is scripts/update.sh, started outside the service because
it restarts it. It rolls back if the new version doesn't come up.
"""
import json
import os
import subprocess
import time

import config

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPDATE_SCRIPT = os.path.join(REPO_DIR, "scripts", "update.sh")
# In saves/ (not /tmp): a service with PrivateTmp would not see the script's /tmp.
STATUS_FILE = os.path.join(REPO_DIR, "saves", "update_status.json")
SERVICE = os.environ.get("PROTOGEN_SERVICE", "protogen")
RUNNING_STATES = ("starting", "fetching", "installing", "restarting", "checking", "rolling_back")
STALE_AFTER_S = 300          # a "running" status older than this is a dead updater


def _git_prefix():
    """The service runs as root but the clone belongs to a normal user: run git
    as that user, so the files it writes keep their owner and git does not
    refuse the repository as "dubious"."""
    cmd = ["git", "-C", REPO_DIR]
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None or geteuid() != 0:
        return cmd
    try:
        import pwd
        owner = pwd.getpwuid(os.stat(REPO_DIR).st_uid).pw_name
    except Exception:
        return cmd
    return cmd if owner == "root" else ["sudo", "-n", "-u", owner] + cmd


def _git(*args, timeout=15):
    """Run git, return (ok, stdout or error text)."""
    try:
        p = subprocess.run(_git_prefix() + list(args), capture_output=True, text=True,
                           timeout=timeout)
    except FileNotFoundError:
        return False, "git is not installed"
    except subprocess.TimeoutExpired:
        return False, "git timed out (no internet?)"
    out = p.stdout.strip()
    return (True, out) if p.returncode == 0 else (False, (p.stderr.strip() or out)[-400:])


def _branch():
    ok, name = _git("rev-parse", "--abbrev-ref", "HEAD")
    return name if ok and name != "HEAD" else None


def version():
    """What is installed. Never raises; `repo: False` when this is not a clone."""
    ok, sha = _git("rev-parse", "--short=8", "HEAD")
    if not ok:
        return {"repo": False, "error": sha}
    info = {"repo": True, "commit": sha, "branch": _branch()}
    ok, line = _git("log", "-1", "--format=%ad|%s", "--date=short")
    if ok and "|" in line:
        info["date"], info["subject"] = line.split("|", 1)
    ok, tag = _git("describe", "--tags", "--always")
    info["version"] = tag if ok else sha
    ok, url = _git("remote", "get-url", "origin")
    info["remote"] = url if ok else None
    ok, dirty = _git("status", "--porcelain", "--untracked-files=no")
    info["modified"] = bool(ok and dirty)
    return info


def check():
    """Ask the server what is new. Needs internet."""
    info = version()
    if not info.get("repo"):
        return {"ok": False, "error": "this copy is not a git clone: " + info.get("error", "")}
    branch = info.get("branch")
    if not branch or not info.get("remote"):
        return {"ok": False, "error": "no branch or no 'origin' remote configured"}
    ok, err = _git("fetch", "--prune", "origin", timeout=60)
    if not ok:
        return {"ok": False, "error": "cannot reach the update server: " + err}
    upstream = f"origin/{branch}"
    ok, behind = _git("rev-list", "--count", f"HEAD..{upstream}")
    if not ok:
        return {"ok": False, "error": f"branch {branch} does not exist on the server"}
    _, ahead = _git("rev-list", "--count", f"{upstream}..HEAD")
    ok, log = _git("log", "--format=%h|%ad|%s", "--date=short", "-n", "20", f"HEAD..{upstream}")
    changes = []
    for line in log.splitlines() if ok else []:
        parts = line.split("|", 2)
        if len(parts) == 3:
            changes.append({"commit": parts[0], "date": parts[1], "subject": parts[2]})
    return {"ok": True, "current": info, "behind": int(behind or 0), "ahead": int(ahead or 0),
            "modified": info["modified"], "changes": changes}


def status():
    """Progress of the last update, as written by scripts/update.sh."""
    try:
        with open(STATUS_FILE) as f:
            st = json.load(f)
    except Exception:
        return {"state": "idle"}
    if st.get("state") in RUNNING_STATES and time.time() - st.get("time", 0) > STALE_AFTER_S:
        st = dict(st, state="error", message="the updater stopped without finishing")
    return st


def _write_status(state, message):
    os.makedirs(os.path.dirname(STATUS_FILE), exist_ok=True)
    tmp = STATUS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"state": state, "message": message, "time": int(time.time())}, f)
    os.replace(tmp, STATUS_FILE)


def start(force=False):
    """Launch the update. `force` throws away local changes to tracked files."""
    if status().get("state") in RUNNING_STATES:
        return {"ok": False, "error": "an update is already running"}
    if not os.path.exists(UPDATE_SCRIPT):
        return {"ok": False, "error": f"{UPDATE_SCRIPT} is missing"}
    args = ["/bin/bash", UPDATE_SCRIPT, REPO_DIR, "force" if force else "ff", SERVICE,
            str(config.WEB_PORT)]
    _write_status("starting", "starting the updater")
    # Own systemd unit: it must survive the restart of our service.
    unit = f"protogen-update-{int(time.time())}"
    try:
        subprocess.Popen(["systemd-run", "--no-block", "--collect", f"--unit={unit}"] + args,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except FileNotFoundError:
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    return {"ok": True}
