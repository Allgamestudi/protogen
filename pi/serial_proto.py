"""USB CDC serial parser for the ESP32-C3 protocol.

Frame format: 0xAA 0x55 type len payload[len] xor
Types:
  0x01 - 16 bytes EQ magnitudes per band
  0x02 - 16 bytes stats
  0x03 - 8 bytes settings
  0xFF - boot signal
"""
import struct
import threading
import time
import serial
import serial.tools.list_ports
import numpy as np

import config
from state import (
    state, lock, stop_event, target_bars, eq_times, ser_handle, log_err,
)


# ---------------- discovery ----------------
def find_esp_port():
    """Return the serial device path for an Espressif USB ID, or None."""
    for p in serial.tools.list_ports.comports():
        if p.vid == config.ESP_USB_VID:
            return p.device
    return None


def send_command(cmd: str) -> None:
    """Send a one-line command to the ESP (e.g. 'M5', 'G20', 'C', 'R')."""
    s = ser_handle.get("ser")
    if s is None:
        return
    try:
        s.write((cmd + "\n").encode())
        s.flush()
    except Exception as e:
        log_err(f"send: {e}")


# ---------------- packet handlers ----------------
def _handle_eq(data: bytes) -> None:
    if len(data) < config.BANDS:
        return
    now = time.time()
    eq_times.append(now)
    b = np.frombuffer(data[: config.BANDS], dtype=np.uint8)
    with lock:
        state["last_eq_ms"] = int(now * 1000)
        state["bars"] = [int(x) for x in b]
        if len(eq_times) > 2:
            dt = eq_times[-1] - eq_times[0]
            if dt > 0:
                state["eq_rate_hz"] = round((len(eq_times) - 1) / dt, 1)
        target_bars[:] = b.astype(np.float32) / 255.0


def _handle_stats(data: bytes) -> None:
    if len(data) < 16:
        return
    up, heap, ncnt, sr, pk = struct.unpack("<IIHHh", data[:14])
    flags = data[15]
    with lock:
        state["esp_uptime_s"]    = round(up / 1000, 1)
        state["esp_free_heap"]   = heap
        state["esp_notify_rate"] = ncnt
        state["esp_sample_rate"] = sr
        state["esp_peak_rms"]    = pk
        state["esp_i2s_ok"]      = bool(flags & 1)
        state["esp_calibrated"]  = bool(flags & 2)
        state["esp_tof_ok"]      = bool(flags & 4)
        state["esp_acc_ok"]      = bool(flags & 8)


def _handle_settings(data: bytes) -> None:
    if len(data) < 8:
        return
    m, g = struct.unpack("<HH", data[:4])
    a, s, c, _ = data[4], data[5], data[6], data[7]
    with lock:
        state["noise_margin"]   = m / 10.0
        state["db_gain"]        = g / 10.0
        state["amp_mult"]       = a
        state["smoothing"]      = s
        state["esp_calibrated"] = bool(c)


def _handle_distance(data: bytes) -> None:
    if len(data) < 4:
        return
    mm = data[0] | (data[1] << 8)
    valid = bool(data[2])
    now_ms = int(time.time() * 1000)
    with lock:
        if valid:
            state["distance_mm"] = mm
            state["last_distance_ms"] = now_ms
        state["distance_valid"] = valid
        state["esp_tof_ok"] = True


def _handle_motion(data: bytes) -> None:
    """Packet 0x05 motion data.

    Two layouts supported:
      8 B:  i16 ax_mg, ay_mg, az_mg, u8 tilt°, u8 flags
     14 B:  i16 ax_mg, ay_mg, az_mg, gx_dps, gy_dps, gz_dps, u8 tilt°, u8 flags
    """
    if len(data) < 8:
        return
    ax, ay, az = struct.unpack("<hhh", data[:6])
    gx = gy = gz = 0
    if len(data) >= 14:
        gx, gy, gz = struct.unpack("<hhh", data[6:12])
        tilt  = data[12]
        flags = data[13]
    else:
        tilt  = data[6]
        flags = data[7]
    now_ms = int(time.time() * 1000)
    with lock:
        state["accel_mg"] = [ax, ay, az]
        state["gyro_dps"] = [gx, gy, gz]
        state["tilt_deg"] = tilt
        state["motion_shake"]    = bool(flags & 0x01)
        if flags & 0x02:
            # Latched - consumer clears.  Also auto-decays after 1 s so a
            # missed consumption (e.g. render thread paused) doesn't leave the
            # face stuck in OUCH forever.
            state["motion_impact"]    = True
            state["motion_impact_ms"] = now_ms
        elif state.get("motion_impact") and (now_ms - state.get("motion_impact_ms", 0)) > 1000:
            state["motion_impact"] = False
        state["motion_freefall"] = bool(flags & 0x04)
        state["motion_flat"]     = bool(flags & 0x08)
        state["last_motion_ms"]  = now_ms
        state["esp_acc_ok"]      = True


def _handle_boot(data: bytes) -> None:
    flags = data[0] if data else 0
    with lock:
        state["boot_count"] += 1
        state["esp_i2s_ok"] = bool(flags & 0x01)
        state["esp_tof_ok"] = bool(flags & 0x02)
        state["esp_acc_ok"] = bool(flags & 0x04)
    log_err(f"ESP boot: flags=0x{flags:02x} (i2s={bool(flags&1)} "
            f"tof={bool(flags&2)} acc={bool(flags&4)})")


HANDLERS = {
    0x01: _handle_eq,
    0x02: _handle_stats,
    0x03: _handle_settings,
    0x04: _handle_distance,
    0x05: _handle_motion,
    0xFF: _handle_boot,
}


# ---------------- main loop ----------------
def _run():
    while not stop_event.is_set():
        try:
            port = find_esp_port()
            if not port:
                with lock:
                    state["connected"] = False
                    state["port"] = None
                time.sleep(1)
                continue
            ser = serial.Serial(
                port, config.BAUD,
                timeout=0.1, write_timeout=0.5,
            )
            ser_handle["ser"] = ser
            with lock:
                state["connected"] = True
                state["port"] = port
                state["reconnects"] += 1
            ser.write(b"?\n")   # request settings immediately

            buf = bytearray()
            last_pkt_t = time.time()
            SILENCE_TIMEOUT = 5.0      # if no packets for this long -> reopen
            while not stop_event.is_set():
                d = ser.read(512)
                if d:
                    buf.extend(d)
                    last_pkt_t = time.time()
                elif time.time() - last_pkt_t > SILENCE_TIMEOUT:
                    # ESP went silent without raising - force a reopen so we
                    # pick up the new ttyACMn after USB re-enumeration.
                    log_err(f"ESP silent {SILENCE_TIMEOUT}s, forcing reopen")
                    try: ser.close()
                    except Exception: pass
                    ser_handle["ser"] = None
                    with lock:
                        state["connected"] = False
                    break
                while len(buf) >= 5:
                    i = buf.find(b"\xAA\x55")
                    if i < 0:
                        del buf[:-1]
                        break
                    if i > 0:
                        del buf[:i]
                    if len(buf) < 5:
                        break
                    type_b = buf[2]
                    length = buf[3]
                    if length > 200:
                        del buf[:2]
                        continue
                    need = 4 + length + 1
                    if len(buf) < need:
                        break
                    payload = bytes(buf[4:4 + length])
                    cs_recv = buf[4 + length]
                    cs_calc = 0
                    for x in buf[: 4 + length]:
                        cs_calc ^= x
                    if cs_calc == cs_recv:
                        handler = HANDLERS.get(type_b)
                        if handler:
                            handler(payload)
                    del buf[:need]
        except Exception as e:
            log_err(str(e)[:100])
            ser_handle["ser"] = None
            with lock:
                state["connected"] = False
            time.sleep(2)


def start():
    """Spawn the serial reader thread."""
    threading.Thread(target=_run, daemon=True, name="serial-reader").start()
