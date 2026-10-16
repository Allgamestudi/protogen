#!/bin/bash
# Запускает Bad Apple плеер на матрице.
# ВАЖНО: должен запускаться через systemd-run --no-block из Flask,
# чтобы НЕ быть в cgroup protogen.service.

LOG=/var/log/protogen/bad_apple.log
mkdir -p "$(dirname $LOG)" 2>/dev/null || true

ts() { date +%H:%M:%S; }

echo "[$(ts)] === Bad Apple launcher (PID $$) ===" >> "$LOG"

BIN=/home/wild/protogen/tools/bad_apple/bad_apple_player
GRAY=/home/wild/protogen-media/bad_apple/bad_apple.gray
MP3=/home/wild/protogen-media/bad_apple/bad_apple.mp3

if [ ! -x "$BIN" ]; then
    echo "[$(ts)] [!] player not built: $BIN" >> "$LOG"
    exit 1
fi

if [ ! -f "$GRAY" ]; then
    echo "[$(ts)] [!] frames not found: $GRAY" >> "$LOG"
    echo "[$(ts)] run prepare_frames.sh first" >> "$LOG"
    exit 1
fi

echo "[$(ts)] stopping protogen.service..." >> "$LOG"
systemctl stop protogen >> "$LOG" 2>&1
sleep 2

echo "[$(ts)] playing..." >> "$LOG"
"$BIN" "$GRAY" >> "$LOG" 2>&1
EXIT=$?
echo "[$(ts)] exited with code $EXIT" >> "$LOG"

echo "[$(ts)] starting protogen.service..." >> "$LOG"
systemctl start protogen >> "$LOG" 2>&1
echo "[$(ts)] done." >> "$LOG"
