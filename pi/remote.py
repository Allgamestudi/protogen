"""4-button wireless remote (YK04 + YK04R receiver) -> protogen actions.

Receiver outputs go HIGH while a button is pressed (active-high, momentary).
We use gpiozero's Button class for clean edge detection + debounce.

Default pin map (BCM):
    GPIO 5  -> next emotion
    GPIO 6  -> previous emotion
    GPIO 12 -> toggle PARTY mode
    GPIO 13 -> recalibrate ESP microphone noise floor

Wiring: receiver VCC->5V/3.3V (whichever the board accepts), GND->GND,
        D0/D1/D2/D3 -> GPIO 5/6/12/13 respectively.
"""
import threading
import time

from state import state, lock, stop_event, mark_ui_dirty
from emotions import EMOTIONS
from serial_proto import send_command


PIN_MAP = {
    5:  "next_emotion",
    6:  "prev_emotion",
    12: "toggle_party",
    13: "recalibrate",
}

DEBOUNCE_S = 0.20


# action handlers
def _cycle_emotion(direction: int) -> None:
    eids = list(EMOTIONS.keys())
    with lock:
        cur = state.get("emotion", eids[0])
        idx = eids.index(cur) if cur in eids else 0
        new = eids[(idx + direction) % len(eids)]
        state["emotion"] = new
        state["mode"]    = "emotion"
        # remember the per-emotion color if any
        saved = state.get("emotion_colors", {}).get(new)
        if isinstance(saved, list) and len(saved) == 3:
            state["color"] = list(saved)
        mark_ui_dirty()


def _toggle_party() -> None:
    with lock:
        state["party"] = not bool(state.get("party"))


def _recalibrate() -> None:
    send_command("C")


ACTIONS = {
    "next_emotion": lambda: _cycle_emotion(+1),
    "prev_emotion": lambda: _cycle_emotion(-1),
    "toggle_party": _toggle_party,
    "recalibrate":  _recalibrate,
}


# runner
def _run() -> None:
    try:
        from gpiozero import Button
    except Exception as e:
        # gpiozero missing or wrong env - remote silently disabled
        print(f"[remote] gpiozero unavailable: {e}; remote disabled")
        return

    buttons = []
    for pin, action_name in PIN_MAP.items():
        fn = ACTIONS.get(action_name)
        if fn is None:
            continue
        try:
            # YK04R drives the pin HIGH while a button is held.
            # pull_up=False sets an internal pull-DOWN, so the pin reads LOW
            # when the receiver is idle and HIGH when it asserts a button -
            # gpiozero infers active_state from this automatically.
            b = Button(pin, pull_up=False, bounce_time=DEBOUNCE_S)
            b.when_pressed = fn
            buttons.append(b)
            print(f"[remote] GPIO {pin} → {action_name}")
        except Exception as e:
            print(f"[remote] failed to bind GPIO {pin}: {e}")

    if not buttons:
        print("[remote] no buttons bound; thread exiting")
        return

    # keep references alive so callbacks survive; sleep until shutdown
    while not stop_event.is_set():
        time.sleep(1.0)

    for b in buttons:
        try:
            b.close()
        except Exception:
            pass


def start() -> None:
    """Spawn the remote-listener thread (no-op if gpiozero is unavailable)."""
    threading.Thread(target=_run, daemon=True, name="remote").start()
