#!/usr/bin/env python3
"""Live console dashboard for the running protogen service.

Polls /api/status 4x per second and prints a single-screen updating dashboard.
Run alongside the service:

    python3 ~/protogen/pi/monitor.py
    # or with a remote Pi:
    python3 monitor.py --host 192.168.31.48

Press Ctrl+C to quit.
"""
import argparse
import json
import os
import sys
import time
from urllib.request import urlopen, Request


def fmt_bool(v, t="●", f="○"):
    return f"\033[32m{t}\033[0m" if v else f"\033[31m{f}\033[0m"


def fmt_bars(bars, width=32):
    """Render 16-band EQ as one-line bar chart."""
    if not bars:
        return "─" * width
    out = []
    for v in bars:
        # 8-level vertical bar via Unicode block characters
        level = int(v / 256 * 8) if v > 0 else 0
        chars = " ▁▂▃▄▅▆▇█"
        out.append(chars[min(8, level)])
    return "".join(out)


def fmt_meter(value, max_val, width=20, color="\033[36m"):
    """Horizontal bar meter 0..max."""
    if max_val <= 0:
        return "─" * width
    filled = int(min(1.0, value / max_val) * width)
    return f"{color}{'█' * filled}\033[0m{'░' * (width - filled)}"


def fmt_zone(zone):
    colors = {
        "near":    "\033[1;31m",  # bright red
        "close":   "\033[1;33m",  # bright yellow
        "mid":     "\033[1;32m",  # bright green
        "far":     "\033[1;34m",  # bright blue
        "unknown": "\033[1;30m",  # dark grey
    }
    c = colors.get(zone, "\033[0m")
    return f"{c}{zone:<7}\033[0m"


def get_status(host, timeout=2.0):
    req = Request(f"http://{host}/api/status")
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def clear():
    # Move cursor to top-left, clear screen below
    sys.stdout.write("\033[H\033[J")


def hide_cursor():  sys.stdout.write("\033[?25l")
def show_cursor():  sys.stdout.write("\033[?25h")


def draw(d, errors_seen):
    """Draw the dashboard from /api/status payload."""
    clear()

    # HEADER
    print("┌─────────────────────────────────────────────────────────────────┐")
    print("│  \033[1;35mPROTOGEN  LIVE MONITOR\033[0m                                       │")
    print("├─────────────────────────────────────────────────────────────────┤")

    # CONNECTION + ESP
    conn = d.get("connected")
    port = d.get("port") or "—"
    eq_rate = d.get("eq_rate_hz", 0)
    print(f"│ ESP  {fmt_bool(conn)} connected   port {port:<14}  "
          f"eq_rate {eq_rate:6.1f} Hz │")

    i2s = d.get("esp_i2s_ok")
    acc = d.get("esp_acc_ok")
    tof = d.get("esp_tof_ok")
    cal = d.get("esp_calibrated")
    boot_n = d.get("boot_count", 0)
    print(f"│      mic {fmt_bool(i2s)}  imu {fmt_bool(acc)}  tof {fmt_bool(tof)}  "
          f"calibrated {fmt_bool(cal)}  boots {boot_n:<3}      │")

    # MODE / EMOTION / BEHAVIOR
    print("├─ STATE ─────────────────────────────────────────────────────────┤")
    mode  = d.get("mode", "?")
    emo   = d.get("emotion", "?")
    bri   = d.get("brightness", 0)
    state = d.get("behavior_state", "?")
    color = d.get("color", [0, 0, 0])
    color_str = f"#{color[0]:02x}{color[1]:02x}{color[2]:02x}"
    state_color = "\033[31m" if state == "ERROR" else "\033[33m" if state == "SLEEP" else "\033[32m"
    print(f"│ mode \033[1;36m{mode:<10}\033[0m  emotion \033[1;35m{emo:<10}\033[0m  "
          f"behavior {state_color}{state:<7}\033[0m       │")
    print(f"│ brightness {bri:3d}%  color {color_str}  "
          f"mouth_open {int(d.get('mouth_open_level', 0) * 100):3d}%             │")

    # AUDIO / EQ
    print("├─ AUDIO ─────────────────────────────────────────────────────────┤")
    bars = d.get("bars", [])
    peak = d.get("esp_peak_rms", 0)
    print(f"│ spectrum  [{fmt_bars(bars, 32)}]   peak {peak:5d}        │")

    # MOTION / IMU
    print("├─ MOTION (IMU) ──────────────────────────────────────────────────┤")
    accel = d.get("accel_mg", [0, 0, 0])
    gyro  = d.get("gyro_dps", [0, 0, 0])
    tilt  = d.get("tilt_deg", 0)
    shake = d.get("motion_shake", False)
    impact = d.get("motion_impact", False)
    freefall = d.get("motion_freefall", False)
    flat = d.get("motion_flat", False)
    print(f"│ accel  x{accel[0]:+5d}  y{accel[1]:+5d}  z{accel[2]:+5d} mg   tilt {tilt:3d}°    │")
    print(f"│ gyro   x{gyro[0]:+5d}  y{gyro[1]:+5d}  z{gyro[2]:+5d} dps  │")
    flag_str = " ".join([
        f"\033[31mSHAKE\033[0m"     if shake    else "shake   ",
        f"\033[31mIMPACT\033[0m"    if impact   else "impact ",
        f"\033[31mFREEFALL\033[0m"  if freefall else "freefall",
        f"\033[32mFLAT\033[0m"      if flat     else "flat ",
    ])
    print(f"│ flags  {flag_str}                                 │")

    # DISTANCE / ToF
    print("├─ PROXIMITY (ToF) ───────────────────────────────────────────────┤")
    dist  = d.get("distance_mm")
    dists = d.get("distance_smoothed_mm")
    zone  = d.get("distance_zone", "unknown")
    valid = d.get("distance_valid", False)
    print(f"│ distance {str(dist if dist is not None else '—'):>6} mm  "
          f"smoothed {str(dists if dists is not None else '—'):>6} mm  "
          f"valid {fmt_bool(valid)}        │")
    print(f"│ zone  {fmt_zone(zone)}                                                │")

    # ERRORS (last few)
    errs = d.get("errors", []) or []
    print("├─ ERRORS (last 4) ───────────────────────────────────────────────┤")
    if not errs:
        print("│ \033[32m(none)\033[0m                                                          │")
    else:
        for e in errs[-4:]:
            line = (e[:62] + "…") if len(e) > 63 else e
            color = "\033[33m" if "Permission" in e else "\033[31m"
            print(f"│ {color}{line:<63}\033[0m │")

    print("└─────────────────────────────────────────────────────────────────┘")
    print(f"  refresh ~4 Hz   |   {time.strftime('%H:%M:%S')}   "
          f"|   Ctrl+C to quit")
    sys.stdout.flush()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="localhost:5001")
    ap.add_argument("--interval", type=float, default=0.25)
    args = ap.parse_args()

    hide_cursor()
    errors_seen = set()
    try:
        while True:
            try:
                d = get_status(args.host)
                draw(d, errors_seen)
            except Exception as e:
                clear()
                print(f"\n  ⚠  cannot reach http://{args.host}/api/status")
                print(f"     {type(e).__name__}: {e}")
                print(f"\n  is the service running?  sudo systemctl status protogen")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        show_cursor()
        print()


if __name__ == "__main__":
    main()
