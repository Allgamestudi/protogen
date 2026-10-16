"""Drive the ESP-attached WS2812 ear rings over USB-CDC.

Each ear is a ring of 13 LEDs. The ESP runs Adafruit_NeoPixel; here we just
hold the desired colour buffer and push it as a single framed packet:

    AA 55 10 4E [L0R L0G L0B ... L12RGB | R0RGB ... R12RGB] XOR

The whole packet is 82 bytes - at 60 Hz that's about 5 KB/s, easily within
USB-CDC throughput.

Usage from another module:
    import ears
    ears.set_all((0, 0, 0))          # all off
    ears.left[3] = (255, 0, 0)       # change one LED
    ears.push()                      # send to ESP

For continuous animation, call push() each frame.
"""
from typing import List, Tuple, Sequence

import serial

from state import ser_handle, log_err


EAR_COUNT     = 13
PKT_TYPE_EARS = 0x10
SYNC1         = 0xAA
SYNC2         = 0x55

# Module-level buffers - mutate directly or via the helpers below.
RGB = Tuple[int, int, int]
left:  List[RGB] = [(0, 0, 0)] * EAR_COUNT
right: List[RGB] = [(0, 0, 0)] * EAR_COUNT


def _clip(v: int) -> int:
    return 0 if v < 0 else (255 if v > 255 else v)


def _build_packet(l: Sequence[RGB], r: Sequence[RGB]) -> bytes:
    """Frame: AA 55 type len payload xor"""
    payload = bytearray(EAR_COUNT * 6)
    j = 0
    for (rr, gg, bb) in l[:EAR_COUNT]:
        payload[j] = _clip(int(rr)); payload[j + 1] = _clip(int(gg)); payload[j + 2] = _clip(int(bb))
        j += 3
    for (rr, gg, bb) in r[:EAR_COUNT]:
        payload[j] = _clip(int(rr)); payload[j + 1] = _clip(int(gg)); payload[j + 2] = _clip(int(bb))
        j += 3
    hdr = bytes([SYNC1, SYNC2, PKT_TYPE_EARS, EAR_COUNT * 6])
    cs = 0
    for v in hdr: cs ^= v
    for v in payload: cs ^= v
    return hdr + bytes(payload) + bytes([cs])


def push() -> None:
    """Send the current left/right buffers to the ESP. Silent on disconnect.

    Wrapped tightly so a USB hiccup during write doesn't propagate up and stall
    the effect runner thread. write_timeout on the serial.Serial object (set in
    serial_proto.py) is 0.5 s, so worst case this blocks half a second.
    """
    s = ser_handle.get("ser")
    if s is None:
        return
    try:
        pkt = _build_packet(left, right)
    except Exception as e:
        log_err(f"ears build: {e}")
        return
    try:
        s.write(pkt)
    except serial.SerialTimeoutException:
        pass            # USB buffer full / ESP busy - drop this frame
    except (serial.SerialException, OSError):
        pass            # USB disconnected mid-write; serial_proto will reconnect
    except Exception as e:
        log_err(f"ears push: {e}")


# convenience setters
def set_all(color: RGB) -> None:
    """Set every LED on both ears to a single colour. Doesn't push."""
    global left, right
    left  = [color] * EAR_COUNT
    right = [color] * EAR_COUNT


def set_left(colors: Sequence[RGB]) -> None:
    """Set left ear. List length up to EAR_COUNT; extra entries ignored."""
    global left
    out = list(left)
    for i, c in enumerate(colors[:EAR_COUNT]):
        out[i] = c
    left = out


def set_right(colors: Sequence[RGB]) -> None:
    global right
    out = list(right)
    for i, c in enumerate(colors[:EAR_COUNT]):
        out[i] = c
    right = out


def clear() -> None:
    """All LEDs off."""
    set_all((0, 0, 0))


def fill_brightness(color: RGB, brightness: float) -> RGB:
    """Helper: scale an RGB tuple by 0..1 brightness."""
    f = max(0.0, min(1.0, float(brightness)))
    return (int(color[0] * f), int(color[1] * f), int(color[2] * f))
