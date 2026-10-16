#!/bin/bash
# Установка DOOM на Pi с выводом на LED-матрицу + DualShock 4
#
# Запуск (на самой Pi):
#   bash ~/protogen/tools/doom/install.sh

set -e

echo "═════════════════════════════════════════════════"
echo "  Protogen DOOM installer"
echo "  doomgeneric → 64×32 LED matrix + DS4 controller"
echo "═════════════════════════════════════════════════"
echo

# ── зависимости ─────────────────────────────────────
echo "[1/5] Установка системных пакетов..."
sudo apt update
sudo apt install -y \
    build-essential git \
    freedoom \
    bluez bluez-tools \
    joystick \
    libsdl2-dev libsdl2-mixer-dev

# ── doomgeneric ─────────────────────────────────────
DG_DIR=~/doomgeneric
if [ ! -d "$DG_DIR" ]; then
    echo "[2/5] Клон doomgeneric..."
    git clone https://github.com/ozkl/doomgeneric.git "$DG_DIR"
else
    echo "[2/5] doomgeneric уже клонирован — пропуск."
fi

# ── копируем наш backend ────────────────────────────
echo "[3/5] Копирование matrix backend..."
PROJECT_DIR=~/protogen
cp "$PROJECT_DIR/tools/doom/doomgeneric_matrix.cpp"  "$DG_DIR/doomgeneric/"
cp "$PROJECT_DIR/tools/doom/Makefile.matrix"         "$DG_DIR/doomgeneric/"

# ── проверяем что rpi-rgb-led-matrix существует ─────
if [ ! -d ~/rpi-rgb-led-matrix ]; then
    echo "[!] rpi-rgb-led-matrix не найден в ~/rpi-rgb-led-matrix"
    echo "[!] Установи его (он же используется протогеном)."
    exit 1
fi

# ── собираем ────────────────────────────────────────
echo "[4/5] Сборка doomgeneric_matrix..."
cd "$DG_DIR/doomgeneric"
make -f Makefile.matrix clean 2>/dev/null || true
make -f Makefile.matrix -j$(nproc)

if [ ! -f "$DG_DIR/doomgeneric/doomgeneric_matrix" ]; then
    echo "[X] Сборка не удалась."
    exit 1
fi

# ── chmod для бинарника (нужны права на /dev/mem) ──
echo "[5/5] Настройка прав..."
sudo chmod +x "$DG_DIR/doomgeneric/doomgeneric_matrix"
sudo chmod +x "$PROJECT_DIR/scripts/run_doom.sh"

echo
echo "═════════════════════════════════════════════════"
echo "  ✅ Установка завершена."
echo "═════════════════════════════════════════════════"
echo
echo "Следующие шаги:"
echo "  1. Подключи DualShock 4 по Bluetooth:"
echo "       sudo bluetoothctl"
echo "       scan on  → зажми PS+SHARE на DS4 → pair / trust / connect"
echo "  2. Перезапусти протоген:"
echo "       sudo systemctl restart protogen"
echo "  3. Открой web-UI и нажми кнопку '🔫 PLAY DOOM'"
echo
