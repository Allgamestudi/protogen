#!/bin/bash
# Install a release archive over a running Protogen, with a way back.
#
#   bash install_release.sh protogen-pi-YYYY-MM-DD.tar.gz [install dir] [service] [port]
#
# What it does, in order, and stops at the first failure:
#   1. unpacks the archive next to the installation and tests it there
#      (unit tests + frame-time benchmark; the panel and the service are not touched)
#   2. saves the current pi/, assets/, scripts/ to saves/backup-<time>.tar.gz
#   3. copies the new files in (saves/ with your settings and own emotions is kept)
#   4. restarts the service and waits until the web page answers
#   5. if it does not come up, puts the backup back and restarts again
set -u
ARCHIVE="${1:?usage: install_release.sh ARCHIVE.tar.gz [dir] [service] [port]}"
DIR="${2:-/home/wild/protogen}"
SERVICE="${3:-protogen}"
PORT="${4:-5001}"
STAMP="$(date +%Y%m%d-%H%M%S)"
NEW="$DIR.new-$STAMP"
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"

say() { echo "== $*"; }
healthy() {
    for _ in $(seq 1 30); do
        sleep 2
        if systemctl is-active --quiet "$SERVICE" \
           && curl -fsS -m 3 "http://127.0.0.1:$PORT/api/status" >/dev/null 2>&1; then
            return 0
        fi
    done
    return 1
}

# sudo is needed from step 2 on: ask for the password now, not after the long test
if [ -n "$SUDO" ] && ! $SUDO -v; then
    echo "sudo cannot ask for the password here. Run over ssh with -t:"
    echo "    ssh -t USER@HOST \"bash install_release.sh $ARCHIVE\""
    exit 1
fi

[ -f "$ARCHIVE" ] || { echo "no such file: $ARCHIVE"; exit 1; }
[ -d "$DIR/pi" ] || { echo "no installation in $DIR"; exit 1; }

say "1/5 unpack and test (nothing is changed yet)"
mkdir -p "$NEW" && tar -xzf "$ARCHIVE" -C "$NEW" || { echo "cannot unpack"; rm -rf "$NEW"; exit 1; }
( cd "$NEW" && python3 -B -m unittest discover -s tests ) || { echo "tests failed, nothing installed"; rm -rf "$NEW"; exit 1; }
( cd "$NEW" && python3 -B scripts/simulator.py --bench )
case $? in
    0) ;;
    2) echo "too slow on this board (see above), nothing installed. To install anyway: FORCE=1"; \
       [ "${FORCE:-0}" = "1" ] || { rm -rf "$NEW"; exit 1; } ;;
    *) echo "the new version reports errors, nothing installed"; rm -rf "$NEW"; exit 1 ;;
esac

say "2/5 backup"
mkdir -p "$DIR/saves"
BACKUP="$DIR/saves/backup-$STAMP.tar.gz"
( cd "$DIR" && $SUDO tar -czf "$BACKUP" --exclude="__pycache__" pi assets scripts $(ls -d tests README.md VERSION 2>/dev/null) ) \
    || { echo "backup failed, nothing installed"; rm -rf "$NEW"; exit 1; }
echo "   $BACKUP"

say "3/5 copy the new files"
( cd "$NEW" && $SUDO cp -a pi assets scripts tests README.md VERSION "$DIR/" ) || { echo "copy failed"; exit 1; }
$SUDO find "$DIR/pi" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null
OWNER="$(stat -c %U:%G "$DIR")"
$SUDO chown -R "$OWNER" "$DIR/pi" "$DIR/assets" "$DIR/scripts" "$DIR/tests" 2>/dev/null
rm -rf "$NEW"

say "4/5 restart $SERVICE"
$SUDO systemctl restart "$SERVICE"
if healthy; then
    say "5/5 done: $(cat "$DIR/VERSION" 2>/dev/null) is running"
    # the service puts the name right by itself at start (pi/hostname_guard.py); give it a moment
    sleep 5
    ADDRESS="$(curl -fsS -m 3 "http://127.0.0.1:$PORT/api/status" 2>/dev/null | sed -n 's/.*"address": *"\([^"]*\)".*/\1/p')"
    echo "   address: ${ADDRESS:-http://$(hostname).local}  (hostname now: $(hostname))"
    exit 0
fi

say "5/5 it did not start: putting the previous version back"
$SUDO journalctl -u "$SERVICE" -n 30 --no-pager 2>/dev/null
( cd "$DIR" && $SUDO tar -xzf "$BACKUP" )
$SUDO systemctl restart "$SERVICE"
healthy && echo "the previous version is running again" || echo "THE PREVIOUS VERSION DOES NOT START EITHER: see journalctl -u $SERVICE"
exit 1
