#!/usr/bin/env bash
# Update the protogen software from its git repository and restart it.
#
#   update.sh <repo dir> <ff|force> [service] [web port]
#
#   ff     only fast-forward; stops if tracked files were edited on the device
#   force  throw local edits of tracked files away and take the server version
#
# Started by the web UI (pi/updater.py) through systemd-run, so that it is NOT
# part of protogen.service: it restarts that service and has to survive it.
# Can also be run by hand:  sudo bash scripts/update.sh ~/protogen ff
#
# Progress goes to saves/update_status.json (read by the web page) and
# saves/update.log. If the new version does not start, the previous commit is
# restored and started again.
#
# Settings (saves/), uploads and anything else that git does not track are
# never touched.
set -u

REPO="${1:?repo dir}"
MODE="${2:-ff}"
SERVICE="${3:-protogen}"
PORT="${4:-5001}"
mkdir -p "$REPO/saves"
STATUS="$REPO/saves/update_status.json"
LOG="$REPO/saves/update.log"
FROM=""
TO=""

exec >>"$LOG" 2>&1
echo
echo "=== $(date '+%F %T') update ($MODE) of $REPO ==="

json_escape() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | tr '\n\r\t' '   '; }

status() {   # status <state> <message>
  echo "[$1] $2"
  printf '{"state": "%s", "message": "%s", "from": "%s", "to": "%s", "time": %s}\n' \
    "$1" "$(json_escape "$2")" "$FROM" "$TO" "$(date +%s)" >"$STATUS.tmp"
  mv -f "$STATUS.tmp" "$STATUS"
  chmod 644 "$STATUS" 2>/dev/null
}

# git as the owner of the clone (the service, and therefore this script, is root)
OWNER="$(stat -c %U "$REPO" 2>/dev/null || echo root)"
as_owner() {
  if [ "$(id -u)" = "0" ] && [ "$OWNER" != "root" ]; then
    sudo -n -u "$OWNER" "$@"
  else
    "$@"
  fi
}
g() { as_owner git -C "$REPO" "$@"; }

have_service() { systemctl cat "$SERVICE" >/dev/null 2>&1; }

restart_service() {
  if have_service; then
    systemctl restart "$SERVICE"
  else
    echo "no $SERVICE.service on this machine: nothing to restart"
  fi
}

healthy() {   # the service is active and the web UI answers
  have_service || return 0
  systemctl is-active --quiet "$SERVICE" || return 1
  if command -v curl >/dev/null 2>&1; then
    curl -fsS -m 4 "http://127.0.0.1:$PORT/api/status" >/dev/null 2>&1
  else
    python3 -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:$PORT/api/status', timeout=4)" 2>/dev/null
  fi
}

wait_healthy() {   # must answer three times in a row within about a minute
  local good=0 i
  for i in $(seq 1 30); do
    sleep 2
    if healthy; then
      good=$((good + 1))
      [ "$good" -ge 3 ] && return 0
    else
      good=0
    fi
  done
  return 1
}

# ── find out what there is ──
if ! g rev-parse --git-dir >/dev/null 2>&1; then
  status error "this copy is not a git clone"
  exit 1
fi
BRANCH="$(g rev-parse --abbrev-ref HEAD)"
FROM="$(g rev-parse --short=8 HEAD)"
FULL_FROM="$(g rev-parse HEAD)"

status fetching "downloading from the server"
fetch() {   # `timeout` cannot run a shell function, so the owner switch is repeated here
  if [ "$(id -u)" = "0" ] && [ "$OWNER" != "root" ]; then
    timeout 120 sudo -n -u "$OWNER" git -C "$REPO" fetch --prune origin
  else
    timeout 120 git -C "$REPO" fetch --prune origin
  fi
}
if ! fetch; then
  status error "cannot reach the update server (no internet?)"
  exit 1
fi
if ! g rev-parse --verify --quiet "origin/$BRANCH" >/dev/null; then
  status error "branch $BRANCH does not exist on the server"
  exit 1
fi
TO="$(g rev-parse --short=8 "origin/$BRANCH")"
if [ "$(g rev-parse HEAD)" = "$(g rev-parse "origin/$BRANCH")" ]; then
  status up_to_date "already the newest version ($FROM)"
  exit 0
fi

# ── install ──
status installing "installing $TO"
if [ "$MODE" = "force" ]; then
  if ! g reset --hard "origin/$BRANCH"; then
    status error "could not switch to $TO"
    exit 1
  fi
elif ! g merge --ff-only "origin/$BRANCH"; then
  status error "files were changed on this device; use 'Update and discard local changes'"
  exit 1
fi

# Python packages, only when the list changed. The interpreter is the one the
# service uses (it may be a virtualenv).
if ! g diff --quiet "$FULL_FROM" HEAD -- pi/requirements.txt; then
  PY="$(systemctl show -p ExecStart --value "$SERVICE" 2>/dev/null | grep -o 'path=[^ ;]*' | head -1 | cut -d= -f2)"
  case "$PY" in *python*) ;; *) PY="$(command -v python3)" ;; esac
  echo "requirements changed, installing with $PY"
  if ! "$PY" -m pip install -r "$REPO/pi/requirements.txt"; then
    "$PY" -m pip install --break-system-packages -r "$REPO/pi/requirements.txt" \
      || echo "pip failed; continuing, the health check decides"
  fi
fi

# ── start it and make sure it runs ──
status restarting "restarting"
restart_service
status checking "waiting for the new version to start"
if wait_healthy; then
  status done "updated from $FROM to $TO"
  exit 0
fi

# ── it does not run: go back ──
status rolling_back "the new version did not start, restoring $FROM"
journalctl -u "$SERVICE" -n 40 --no-pager 2>/dev/null
g reset --hard "$FULL_FROM"
restart_service
if wait_healthy; then
  status rolled_back "version $TO did not start; $FROM was restored"
else
  status error "version $TO did not start and $FROM does not start either; see $LOG"
fi
exit 1
