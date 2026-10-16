#!/bin/bash
# Запускает DOOM на LED-матрице.
#
# ВАЖНО: этот скрипт ДОЛЖЕН запускаться через systemd-run --no-block
# из Flask, чтобы НЕ быть в cgroup protogen.service — иначе когда мы
# его остановим, systemd убьёт и нас самих.
#
# Запуск из Flask:
#    subprocess.Popen([
#        "systemd-run", "--no-block", "--unit=protogen-doom",
#        "/bin/bash", "/home/wild/protogen/scripts/run_doom.sh"
#    ])
#
# Запуск вручную (для теста):
#    sudo systemd-run --no-block --unit=protogen-doom \
#        /bin/bash /home/wild/protogen/scripts/run_doom.sh

LOG=/var/log/protogen/doom.log
mkdir -p "$(dirname $LOG)" 2>/dev/null || true

ts() { date +%H:%M:%S; }

echo "[$(ts)] === DOOM launcher (PID $$) ===" >> "$LOG"

# 1) Останавливаем протоген (мы НЕ в его cgroup, нас не убьёт)
echo "[$(ts)] stopping protogen.service..." >> "$LOG"
systemctl stop protogen >> "$LOG" 2>&1
sleep 2

# 2) Ищем бинарь и WAD
DOOM_BIN=/home/wild/doomgeneric/doomgeneric/doomgeneric_matrix
WAD=/usr/share/games/doom/freedoom1.wad

if [ ! -x "$DOOM_BIN" ]; then
    echo "[$(ts)] [!] $DOOM_BIN не найден или не исполняемый" >> "$LOG"
    systemctl start protogen >> "$LOG" 2>&1
    exit 1
fi

if [ ! -f "$WAD" ]; then
    for alt in /usr/share/games/doom/freedoom2.wad \
               /usr/share/games/doom/freedm.wad \
               /usr/share/games/freedoom/freedoom1.wad \
               /usr/share/games/freedoom/freedoom2.wad; do
        if [ -f "$alt" ]; then WAD=$alt; break; fi
    done
fi

if [ ! -f "$WAD" ]; then
    echo "[$(ts)] [!] WAD не найден" >> "$LOG"
    systemctl start protogen >> "$LOG" 2>&1
    exit 1
fi

# 3) Запускаем DOOM сразу на E1M1, сложность 2 — пропускаем меню
#    (menu UI не читается на 64×32, проще стартануть в игру напрямую).
echo "[$(ts)] starting DOOM with WAD=$WAD..." >> "$LOG"
"$DOOM_BIN" -iwad "$WAD" -warp 1 1 -skill 2 >> "$LOG" 2>&1
EXIT=$?
echo "[$(ts)] DOOM exited with code $EXIT" >> "$LOG"

# 4) Возвращаем протоген
echo "[$(ts)] starting protogen.service..." >> "$LOG"
systemctl start protogen >> "$LOG" 2>&1
echo "[$(ts)] done." >> "$LOG"
