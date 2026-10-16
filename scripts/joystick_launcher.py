#!/usr/bin/env python3
"""
Слушает DS4 на /dev/input/js0. По кнопкам запускает разные приложения:

  PS (BUTTON 10), удержание >= 1.0s   -> DOOM
  Circle (BUTTON 1), удержание >= 1.0s -> Bad Apple

Оба стартуют через systemd-run в отдельном cgroup'е, чтобы остановка
protogen.service не убила их. Внутри своих приложений тапы (короткие
нажатия) обрабатываются самим приложением (например тап PS в DOOM =
выход), поэтому здесь мы триггеримся только на длительное удержание.
"""
import os
import struct
import subprocess
import time

JS_DEV = "/dev/input/js0"

# Формат события js (linux/joystick.h):
#   __u32 time, __s16 value, __u8 type, __u8 number
EVENT_FMT = "IhBB"
EVENT_SIZE = struct.calcsize(EVENT_FMT)

JS_EVENT_BUTTON = 0x01
JS_EVENT_INIT   = 0x80

HOLD_S = 1.0

# button_number -> (name, cmd)
TRIGGERS = {
    10: ("DOOM",      ["systemd-run", "--no-block", "--collect",
                       "--unit=protogen-doom",
                       "/bin/bash", "/home/wild/protogen/scripts/run_doom.sh"]),
    1:  ("Bad Apple", ["systemd-run", "--no-block", "--collect",
                       "--unit=protogen-bad-apple",
                       "/bin/bash", "/home/wild/protogen/scripts/run_bad_apple.sh"]),
}


def wait_for_device():
    while not os.path.exists(JS_DEV):
        time.sleep(2)


def main():
    while True:
        wait_for_device()
        try:
            with open(JS_DEV, "rb") as f:
                print(f"[joy] opened {JS_DEV}", flush=True)
                # {button_number: press_start_time}
                hold_start = {}
                while True:
                    buf = f.read(EVENT_SIZE)
                    if len(buf) != EVENT_SIZE:
                        break
                    _, value, ev_type, number = struct.unpack(EVENT_FMT, buf)
                    ev_type &= ~JS_EVENT_INIT
                    if ev_type != JS_EVENT_BUTTON:
                        continue
                    if number not in TRIGGERS:
                        continue

                    name, cmd = TRIGGERS[number]

                    if value:
                        hold_start[number] = time.time()
                    else:
                        start = hold_start.pop(number, None)
                        if start is None:
                            continue
                        held = time.time() - start
                        print(f"[joy] button {number} ({name}) released "
                              f"after {held:.2f}s", flush=True)
                        if held >= HOLD_S:
                            print(f"[joy] launching {name}", flush=True)
                            subprocess.Popen(cmd)
                            time.sleep(5)   # игнор событий на время старта
                            hold_start.clear()
        except (OSError, IOError) as e:
            print(f"[joy] read error: {e} — reopening", flush=True)
            time.sleep(2)


if __name__ == "__main__":
    main()
