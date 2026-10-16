# 🔫 DOOM on Protogen LED matrix

Easter-egg: запускает DOOM на 64×32 HUB75 матрице протогена с управлением
DualShock 4 по Bluetooth. Кнопка `🔫 PLAY DOOM` в web-UI (раздел System).

## Что внутри

| Файл | Что |
|---|---|
| `install.sh` | Однократная установка: ставит зависимости, клонирует doomgeneric, копирует наш backend, собирает |
| `doomgeneric_matrix.cpp` | Кастомный renderer для doomgeneric: рендер 320×200 → 64×32 + ввод с DS4 |
| `Makefile.matrix` | Сборка с линковкой rpi-rgb-led-matrix |
| `README.md` | Этот файл |

Плюс в основном проекте:

| Файл | Что |
|---|---|
| `scripts/run_doom.sh` | Останавливает protogen → запускает DOOM → возвращает protogen |
| `pi/web_app.py` | Endpoint `/api/system/launch_doom` + кнопка в UI |

## Установка (один раз)

На Pi:

```bash
ssh wild@proto.local
bash ~/protogen/tools/doom/install.sh
```

Скрипт сам поставит `freedoom`, склонирует doomgeneric, скопирует backend,
соберёт `doomgeneric_matrix`. Занимает ~5 минут.

## DualShock 4 — pairing

```bash
sudo systemctl start bluetooth
sudo bluetoothctl
```

В интерактиве:
```
power on
agent on
default-agent
scan on
```

Зажми **PS + SHARE** на DS4 (3-5 сек) — светодиод быстро мигает.

Появится `[NEW] Device A1:B2:... Wireless Controller` — скопируй MAC, дальше:
```
pair A1:B2:...
trust A1:B2:...
connect A1:B2:...
exit
```

Проверка: `jstest /dev/input/js0` — должны меняться значения при нажатии кнопок.

### Авто-коннект после reboot

```bash
sudo tee /etc/systemd/system/ds4-connect.service > /dev/null << EOF
[Unit]
Description=Auto-connect DualShock 4
After=bluetooth.service

[Service]
Type=oneshot
ExecStart=/usr/bin/bluetoothctl connect A1:B2:C3:D4:E5:F6
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable ds4-connect
```
(MAC замени на свой)

## Раскладка кнопок DS4

| DS4 | DOOM |
|---|---|
| Левый стик ↑↓ | Идти вперёд / назад |
| Левый стик ←→ | Поворот |
| L1 | Strafe ← |
| R1 | Strafe → |
| L2 | Бег |
| R2 / × | Огонь |
| ○ | Использовать (открыть дверь) |
| △ | Карта |
| □ | Использовать (alt) |
| Options | Меню / Enter |
| Share | Esc |

Раскладка задана в `doomgeneric_matrix.cpp` в массивах `g_buttons[]` и
`g_axes[]` — меняй если хочешь свою раскладку.

## Запуск

Web-UI → раздел **Settings** → кнопка `🔫 PLAY DOOM`. Подтверди → подожди 3-4
секунды → DOOM запускается на матрице.

Выход: в DOOM → **Options → Quit** (или зажми Share). Протоген вернётся
автоматически.

## Что увидишь

64×32 пикселя для DOOM — это **очень мало**. Будут видны:
- Цветовые "пятна" движений
- Силуэт врага близко
- Вспышки выстрелов
- Стены коридора

НЕ будет видно:
- HUD (здоровье, патроны)
- Текст меню
- Детали врагов

Это арт-проект "DOOM на минимальном экране". Играть всерьёз — нет, показать
друзьям — да 🔥

## Производительность

Pi 4 + matrix 64×32 = ~25-30 FPS, играбельно.

## Troubleshooting

| Проблема | Решение |
|---|---|
| `make` ругается на `led-matrix.h` | Проверь путь в `Makefile.matrix` (`$(HOME)/rpi-rgb-led-matrix/include`) |
| Матрица не включается | Запуск только от root (`sudo`). Скрипт `run_doom.sh` это делает |
| DS4 не подключается | `sudo systemctl restart bluetooth`, повтори pairing |
| Картинка тёмная | Понизь `brightness` в `DG_Init()` ещё ниже (50-60), или повысь до 100 |
| `/dev/input/js0` пустой | DS4 не подключён. Проверь `bluetoothctl info MAC` → Connected: yes |
| Протоген не возвращается | `sudo systemctl start protogen` вручную. Лог: `/var/log/protogen/doom.log` |
| DOOM зависает | `sudo pkill doomgeneric_matrix && sudo systemctl start protogen` |

## Логи

```bash
tail -f /var/log/protogen/doom.log
```

Шоу launch / exit, и stdout/stderr DOOM-процесса.
