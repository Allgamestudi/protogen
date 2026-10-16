"""Run the real render loop (pi/matrix_renderer.py) on a PC.

The panel is a tk window, the ESP sound is generated. Also serves the web UI.

    python scripts/simulator.py              window + web page
    python scripts/simulator.py --no-window  web page only
    python scripts/simulator.py --record out.gif --seconds 14
    python scripts/simulator.py --bench      frame time on this machine (run it on the Pi)

Keys: 1..9 emotion, space pause, . step, Up jump.
"""
import argparse
from collections import deque
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SOUNDS = ("silence", "speech", "music", "manual")


# time
class SimClock:
    """Stands in for the `time` module inside the firmware.

    In a window it follows real time, scaled by `speed` and frozen while paused:
    slow motion shows the very frames the panel would show, only longer.
    Without a window it moves one frame per rendered frame, as fast as it goes.
    """

    def __init__(self, realtime):
        self.realtime = realtime
        self.speed = 1.0
        self.paused = False
        self._now = 1000.0
        self._real = time.perf_counter()
        self._epoch = time.time() - self._now
        self._guard = threading.Lock()

    def monotonic(self):
        with self._guard:
            real = time.perf_counter()
            if self.realtime and not self.paused:
                self._now += (real - self._real) * self.speed
            self._real = real
            return self._now

    def time(self):
        return self._epoch + self.monotonic()

    def sleep(self, seconds):
        if self.realtime and seconds > 0:
            time.sleep(seconds / self.speed)

    def tick(self, seconds):
        with self._guard:
            self._now += seconds

    def __getattr__(self, name):
        return getattr(time, name)


# sound
def sound_bars(kind, t, level, bands):
    """What the ESP would send: the level of each frequency band, 0..1."""
    i = np.arange(bands, dtype=np.float32)
    bars = np.zeros(bands, dtype=np.float32)
    if kind == "speech":
        syllable = max(0.0, math.sin(2 * math.pi * 4.2 * t + 1.3 * math.sin(2 * math.pi * 0.7 * t))) ** 1.5
        phrase = 1.0 if math.sin(2 * math.pi * 0.28 * t) > -0.35 else 0.0     # pauses between phrases
        centre = 5.0 + 2.0 * math.sin(2 * math.pi * 1.1 * t)
        bars = level * syllable * phrase * np.exp(-((i - centre) / 2.2) ** 2)
        hiss = max(0.0, math.sin(2 * math.pi * 0.9 * t)) ** 6 * phrase             # "s", "sh" now and then
        hum = max(0.0, math.sin(2 * math.pi * 0.9 * t + 2.2)) ** 6 * phrase         # "o", "u"
        bars = bars * (1.0 - 0.8 * max(hiss, hum))
        bars[10:] += level * 0.8 * hiss
        bars[1:4] += level * 0.9 * hum
    elif kind == "music":
        beat = (t * 2.0) % 1.0                                                # 120 beats per minute
        bars = level * 0.35 * (0.5 + 0.5 * np.sin(2 * math.pi * (0.5 + i * 0.13) * t + i))
        bars[:3] = level * math.exp(-beat * 8.0)
        bars[11:] = level * 0.5 * math.exp(-((t * 4.0) % 1.0) * 12.0)
    elif kind == "manual":
        bars[2:10] = level
    return np.clip(bars, 0.0, 1.0).astype(np.float32)


GESTURES = ("nod", "shake", "pet", "boop", "knock")


# the firmware, without hardware
class Simulator:
    """Loads the firmware modules and plays the part of the LED matrix."""

    def __init__(self, polygons=False, boot=False, realtime=True):
        self.clock = SimClock(realtime)
        self.sound = "speech"
        self.level = 0.6
        self.frame = None               # the last finished frame, HxWx3 uint8
        self.frame_no = 0
        self.render_ms = 0.0            # time the firmware spent on the last frame
        self.history = deque(maxlen=300)
        self.on_frame = None            # called from the render thread after every frame
        self.step = threading.Event()
        self.brightness = 100
        self.link = True                # "the sensor board is plugged in"
        self.web_url = None
        self._last_swap = None
        self._load(polygons, boot)

    def _load(self, polygons, boot):
        fake = ModuleType("rgbmatrix")
        fake.RGBMatrix = lambda options=None: self
        fake.RGBMatrixOptions = SimpleNamespace
        serial = ModuleType("serial_proto")
        serial.send_command = lambda command: None
        sys.modules["rgbmatrix"] = fake
        sys.modules["serial_proto"] = serial
        try:
            import serial as _pyserial      # noqa: F401  (ears.py wants pyserial)
        except ImportError:
            stub = ModuleType("serial")
            stub.SerialException = stub.SerialTimeoutException = OSError
            sys.modules["serial"] = stub
        sys.path.insert(0, str(ROOT / "pi"))

        with patch("os.makedirs"):          # config creates /tmp/led_uploads
            import config
        if polygons:
            config.EMOTION_ART = False
        import state
        import emotions
        import animations
        import boot_animation
        import dino_game
        import postfx
        import matrix_renderer

        for module in (emotions, animations, boot_animation, dino_game, postfx, matrix_renderer):
            if getattr(module, "time", None) is time:
                module.time = self.clock
        if not boot:
            boot_animation.BootSequence = lambda: SimpleNamespace(done=True)
        state.ser_handle["ser"] = SimpleNamespace(write=len)    # "the ESP is plugged in"
        state.state["connected"] = True
        self.config, self.shared, self.emotions, self.renderer = config, state, emotions, matrix_renderer
        self.fps = config.TARGET_FPS

    # the interface of rgbmatrix: matrix and canvas in one object
    def CreateFrameCanvas(self):
        return self

    def SetImage(self, image):
        self._image = image

    def SwapOnVSync(self, canvas):
        real = time.perf_counter()
        if self._last_swap is not None:
            self.render_ms = (real - self._last_swap) * 1000.0
        self.frame = np.asarray(self._image.convert("RGB"))
        self.frame_no += 1
        self.history.append(self.frame)
        if self.on_frame:
            self.on_frame(self)

        stop = self.shared.stop_event
        if self.clock.realtime:
            while self.clock.paused and not self.step.is_set() and not stop.is_set():
                time.sleep(0.01)
            if self.clock.paused and self.step.is_set():
                self.step.clear()
                self.clock.tick(1.0 / self.fps)
        else:
            self.clock.tick(1.0 / self.fps)

        with self.shared.lock:
            self.shared.target_bars[:] = sound_bars(
                self.sound, self.clock.monotonic(), self.level, self.config.BANDS)
            self.shared.state["connected"] = self.link
        self._last_swap = time.perf_counter()
        return canvas

    def Clear(self):
        pass

    # control, the way the web page does it
    def set(self, **values):
        with self.shared.lock:
            self.shared.state.update(values)

    def setting(self, key, value):
        with self.shared.lock:
            self.shared.state["settings"][key] = value

    def get(self, key, default=None):
        with self.shared.lock:
            return self.shared.state.get(key, default)

    def gesture(self, kind):
        """Play the sensor readings of a gesture into the firmware (about 2 s)."""
        def run():
            ms = lambda: int(self.clock.time() * 1000)
            t0 = time.perf_counter()
            while time.perf_counter() - t0 < (2.5 if kind == "pet" else 1.2):
                t = time.perf_counter() - t0
                values = {"esp_acc_ok": True}
                if kind in ("nod", "shake"):
                    rate = 150.0 * math.sin(2 * math.pi * 3.0 * t)
                    values["gyro_dps"] = [rate, 0, 0] if kind == "nod" else [0, 0, rate]
                elif kind in ("pet", "boop"):
                    near = t < (2.2 if kind == "pet" else 0.25)
                    values.update(distance_mm=70 if near else 900, distance_valid=True,
                                  last_distance_ms=ms())
                elif kind == "knock":
                    values["motion_impact"] = t < 0.05 or 0.45 < t < 0.5
                self.set(**values)
                time.sleep(0.02)
            self.set(gyro_dps=[0, 0, 0], distance_valid=False, motion_impact=False)
        threading.Thread(target=run, daemon=True).start()

    def load_picture(self, path):
        import image_convert
        frames, info = image_convert.convert_upload(Path(path).read_bytes(), self.config.W, self.config.H)
        with self.shared.lock:
            self.shared.uploaded_gif_frames[:] = frames if info["animated"] else []
            self.shared.uploaded_image[:] = frames[0][0]
            self.shared.state["has_image"] = True
            self.shared.state["mode"] = "image"
        return info

    def start(self):
        threading.Thread(target=self.renderer._run, daemon=True, name="matrix-renderer").start()

    # the web page of the firmware, with one more tab for what the hardware would do
    def sim_state(self):
        import wifi
        return {"sound": self.sound, "level": round(self.level, 3), "speed": self.clock.speed,
                "paused": bool(self.clock.paused), "link": self.link, "net": wifi.fake["net"]}

    def sim_apply(self, d):
        if d.get("sound") in SOUNDS:
            self.sound = d["sound"]
        if "level" in d:
            self.level = min(1.0, max(0.0, float(d["level"])))
        if "speed" in d:
            self.clock.speed = min(4.0, max(0.05, float(d["speed"])))
        if "paused" in d and bool(d["paused"]) != bool(self.clock.paused):
            self.clock.monotonic()              # settle the clock before the switch
            self.clock.paused = bool(d["paused"])
        if "link" in d:
            self.link = bool(d["link"])
        if d.get("gesture") in GESTURES:
            self.gesture(d["gesture"])
        if d.get("net") in ("online", "no_internet", "no_wifi"):
            import wifi
            wifi.fake["net"] = d["net"]

    def serve(self, port=8080):
        """Start the firmware's own web interface on this PC. Returns its address."""
        from socketserver import ThreadingMixIn
        from wsgiref.simple_server import make_server, WSGIRequestHandler, WSGIServer
        import ears_effect
        import web_app

        web_app.SIMULATED = True
        web_app.wifi.fake = {"net": "online", "ssid": "Home"}      # a pretend network
        sim = self

        @web_app.app.route("/api/sim", methods=["GET", "POST"])
        def api_sim():
            if web_app.request.method == "POST":
                sim.sim_apply(web_app.request.get_json(silent=True) or {})
            return web_app.jsonify(sim.sim_state())

        class Server(ThreadingMixIn, WSGIServer):
            daemon_threads = True

        class Quiet(WSGIRequestHandler):
            def log_message(self, *args):
                pass

        last = None
        for candidate in range(port, port + 10):    # the port may be taken
            try:
                server = make_server("127.0.0.1", candidate, web_app.app,
                                     server_class=Server, handler_class=Quiet)
                break
            except OSError as e:
                last = e
        else:
            raise last
        threading.Thread(target=server.serve_forever, daemon=True, name="web").start()
        ears_effect.start()
        self.web_url = f"http://127.0.0.1:{candidate}/"
        return self.web_url

    def stop(self):
        self.shared.stop_event.set()


# picture of the panel
def led_mask(height, width, scale):
    """Round LEDs with dark gaps, as one float mask for the enlarged frame."""
    yy, xx = np.mgrid[0:scale, 0:scale]
    centre = (scale - 1) / 2.0
    dot = np.clip(scale * 0.43 - np.hypot(yy - centre, xx - centre) + 0.5, 0.0, 1.0)
    return np.tile(dot, (height, width)).astype(np.float32)[..., None]


def enlarge(frame, scale, mask=None):
    big = np.repeat(np.repeat(frame, scale, axis=0), scale, axis=1)
    if mask is not None:
        big = (big * mask).astype(np.uint8)
    return Image.fromarray(big)


def save_gif(frames, path, fps, scale=4):
    pictures = [enlarge(f, scale) for f in frames]
    pictures[0].save(path, save_all=True, append_images=pictures[1:],
                     duration=int(round(1000 / fps)), loop=0)


# without a window
def record(args):
    sim = Simulator(polygons=args.polygons, boot=args.boot, realtime=False)
    sim.sound = args.sound
    ids = list(sim.emotions.EMOTIONS)
    total = int(args.seconds * sim.fps)
    hold = max(1, total // len(ids))
    frames = []

    def on_frame(s):
        frames.append(s.frame)
        s.set(emotion=ids[min(len(frames) // hold, len(ids) - 1)], connected=True)
        if len(frames) >= total:
            s.stop()

    sim.on_frame = on_frame
    sim.set(emotion=ids[0])
    sim.renderer._render_loop()
    save_gif(frames, args.record, sim.fps)
    errors = sim.get("errors")
    print(f"{len(frames)} frames, {len(ids)} emotions -> {args.record}")
    if errors:
        print("firmware errors:", *errors, sep="\n  ")
    return 1 if errors else 0


def bench(args):
    """Time the render loop on this machine with everything switched on."""
    import platform
    sim = Simulator(polygons=args.polygons, realtime=False)
    sim.sound = "music"                     # the heaviest case: beats, flashes, mouth, both eyes
    ids = list(sim.emotions.EMOTIONS)
    total = int(args.seconds * sim.fps)
    stamps = []

    def on_frame(s):
        stamps.append(time.perf_counter())
        s.set(emotion=ids[(len(stamps) // 45) % len(ids)], connected=True)
        if len(stamps) >= total:
            s.stop()

    sim.on_frame = on_frame
    sim.setting("eye_style", 1)             # eyes with a pupil: a little more to draw
    started = time.perf_counter()
    sim.renderer._render_loop()
    cost = np.diff(stamps[30:]) * 1000.0                    # skip the warm-up
    budget = 1000.0 / sim.fps
    median, p95, worst = float(np.median(cost)), float(np.percentile(cost, 95)), float(cost.max())
    errors = sim.get("errors")
    print(f"{platform.machine()}, Python {platform.python_version()}, NumPy {np.__version__}, "
          f"Pillow {Image.__version__}")
    print(f"faces from artwork: {len(sim.emotions.ART_FACES)} of {len(ids)}; "
          f"with a pupil: {len(sim.emotions.STYLE_FACES.get('anatomical', {}))}")
    print(f"frame: median {median:.1f} ms, 95 % under {p95:.1f} ms, worst {worst:.1f} ms "
          f"(budget {budget:.1f} ms at {sim.fps} fps, {len(cost)} frames, {time.perf_counter() - started:.0f} s)")
    if errors:
        print("FIRMWARE ERRORS:", *errors, sep="\n  ")
        return 1
    # the panel refresh itself also takes CPU on the Pi, so leave room for it
    if p95 <= budget * 0.6:
        print("OK: enough headroom.")
        return 0
    if median <= budget * 0.8:
        print("TIGHT: it will mostly hold the frame rate but may stutter. To make it lighter, in the web "
              "settings switch off: Eyes look one way, Winks, Mouth shapes, Aura; or set fx_bloom to 0.")
        return 0
    print("TOO SLOW for this frame rate. Lower TARGET_FPS in pi/config.py to 20, or switch off "
          "Eyes look one way / Winks / Mouth shapes / Aura in the web settings, and run this again.")
    return 2


# window
def window(args):
    import tkinter as tk
    from tkinter import colorchooser, filedialog, messagebox, ttk
    from PIL import ImageTk

    sim = Simulator(polygons=args.polygons, boot=args.boot, realtime=True)
    sim.sound = args.sound
    if args.web:
        try:
            print("веб-интерфейс:", sim.serve(args.web))
        except Exception as e:
            print("веб-интерфейс не запустился:", e)
    cfg = sim.config
    scale = args.scale
    width, height = cfg.W * cfg.CHAIN_LENGTH, cfg.H
    mask = led_mask(height, width, scale) if scale >= 4 else None
    ids = list(sim.emotions.EMOTIONS)

    root = tk.Tk()
    root.title("Protogen — симулятор" + (" (полигоны)" if args.polygons else ""))
    root.configure(bg="#111")
    root.resizable(False, False)
    panel = tk.Label(root, bg="black", bd=0)
    panel.pack(padx=8, pady=8)

    def row():
        frame = ttk.Frame(root)
        frame.pack(fill="x", padx=8, pady=2)
        return frame

    # emotions
    r = row()
    emotion = tk.StringVar(value=sim.get("emotion"))
    for n, eid in enumerate(ids):
        ttk.Radiobutton(r, text=f"{n + 1} {sim.emotions.EMOTIONS[eid][1]}", value=eid, variable=emotion,
                        command=lambda: sim.set(emotion=emotion.get(), mode="emotion")).pack(side="left")
        root.bind(str(n + 1), lambda e, eid=eid: (emotion.set(eid), sim.set(emotion=eid, mode="emotion")))

    # mode, colour
    r = row()
    mode = tk.StringVar(value=sim.get("mode"))
    effect = tk.StringVar(value=sim.get("effect"))
    party = tk.BooleanVar(value=False)
    link = tk.BooleanVar(value=True)
    ttk.Label(r, text="Режим").pack(side="left")
    box = ttk.Combobox(r, textvariable=mode, width=9, state="readonly",
                       values=("emotion", "eq", "music", "text", "dino", "image"))
    box.pack(side="left", padx=4)
    box.bind("<<ComboboxSelected>>", lambda e: sim.set(mode=mode.get()))
    ttk.Label(r, text="Эффект").pack(side="left")
    box = ttk.Combobox(r, textvariable=effect, width=12, state="readonly",
                       values=("solid", "gradient", "rainbow", "audio", "spectrogram"))
    box.pack(side="left", padx=4)
    box.bind("<<ComboboxSelected>>", lambda e: sim.set(effect=effect.get()))

    def pick_colour():
        rgb, _ = colorchooser.askcolor(color="#%02x%02x%02x" % tuple(sim.get("color")))
        if rgb:
            sim.set(color=[int(c) for c in rgb])

    def pick_picture():
        path = filedialog.askopenfilename(title="Картинка или GIF")
        if not path:
            return
        try:
            info = sim.load_picture(path)
        except Exception as e:
            messagebox.showerror("Не получилось", str(e))
            return
        mode.set("image")
        note.set(f"{os.path.basename(path)}: кадров {info.get('frames', 1)}")

    ttk.Button(r, text="Цвет…", command=pick_colour).pack(side="left", padx=4)
    ttk.Button(r, text="Картинка/GIF…", command=pick_picture).pack(side="left")
    ttk.Checkbutton(r, text="Party", variable=party,
                    command=lambda: sim.set(party=party.get())).pack(side="left", padx=6)
    ttk.Checkbutton(r, text="ESP на связи", variable=link,
                    command=lambda: setattr(sim, "link", link.get())).pack(side="left")
    if sim.web_url:
        import webbrowser
        ttk.Button(r, text="Веб-страница ↗",
                   command=lambda: webbrowser.open(sim.web_url)).pack(side="left", padx=6)

    # reactions
    r = row()
    ttk.Label(r, text="Жесты").pack(side="left")
    for kind, text in (("nod", "кивок"), ("shake", "мотание"), ("pet", "погладить"),
                       ("boop", "тык в нос"), ("knock", "два стука")):
        ttk.Button(r, text=text, command=lambda k=kind: sim.gesture(k)).pack(side="left", padx=2)
    ttk.Button(r, text="😉 ◀", width=5, command=lambda: sim.set(wink_request="left")).pack(side="left", padx=(10, 2))
    ttk.Button(r, text="▶ 😉", width=5, command=lambda: sim.set(wink_request="right")).pack(side="left")
    anatomical = tk.BooleanVar(value=False)
    ttk.Checkbutton(r, text="глаза со зрачком", variable=anatomical,
                    command=lambda: sim.setting("eye_style", int(anatomical.get()))).pack(side="left", padx=4)
    for key, text in (("auto_emotion", "эмоции по звуку"), ("auto_gain", "автогромкость")):
        var = tk.BooleanVar(value=True)
        ttk.Checkbutton(r, text=text, variable=var,
                        command=lambda k=key, v=var: sim.setting(k, v.get())).pack(side="left", padx=4)
    root.bind("w", lambda e: sim.set(wink_request="left"))

    # sound
    r = row()
    sound = tk.StringVar(value=sim.sound)
    ttk.Label(r, text="Звук").pack(side="left")
    for value, text in zip(SOUNDS, ("тишина", "речь", "музыка", "вручную")):
        ttk.Radiobutton(r, text=text, value=value, variable=sound,
                        command=lambda: setattr(sim, "sound", sound.get())).pack(side="left", padx=2)
    ttk.Label(r, text="громкость").pack(side="left", padx=(10, 2))
    level = ttk.Scale(r, from_=0.0, to=1.0, length=220,
                      command=lambda v: setattr(sim, "level", float(v)))
    level.set(sim.level)
    level.pack(side="left")
    ttk.Label(r, text="живость").pack(side="left", padx=(10, 2))
    lively = ttk.Scale(r, from_=0.0, to=2.0, length=120,
                       command=lambda v: sim.setting("liveliness", float(v)))
    lively.set(1.0)
    lively.pack(side="left")

    # time
    r = row()
    speed = tk.DoubleVar(value=1.0)
    ttk.Label(r, text="Скорость").pack(side="left")
    for value in (1.0, 0.25, 0.1):
        ttk.Radiobutton(r, text=f"×{value:g}", value=value, variable=speed,
                        command=lambda: setattr(sim.clock, "speed", speed.get())).pack(side="left", padx=2)
    pause_text = tk.StringVar(value="Пауза")

    def pause(event=None):
        sim.clock.monotonic()               # settle the clock before the switch
        sim.clock.paused = not sim.clock.paused
        pause_text.set("Дальше" if sim.clock.paused else "Пауза")

    def one_frame(event=None):
        if not sim.clock.paused:
            pause()
        sim.step.set()

    def save_last():
        frames = list(sim.history)
        path = filedialog.asksaveasfilename(defaultextension=".gif", filetypes=[("GIF", "*.gif")])
        if path and frames:
            save_gif(frames, path, sim.fps)
            note.set(f"сохранено кадров: {len(frames)}")

    def other_faces():
        command = [sys.executable, os.path.abspath(__file__), "--scale", str(scale), "--sound", sim.sound]
        if not args.polygons:
            command.append("--polygons")
        subprocess.Popen(command)
        close()

    ttk.Button(r, textvariable=pause_text, command=pause).pack(side="left", padx=(10, 2))
    ttk.Button(r, text="Кадр →", command=one_frame).pack(side="left")
    ttk.Button(r, text="GIF последних 10 с…", command=save_last).pack(side="left", padx=10)
    ttk.Button(r, text="Оригинальные лица" if args.polygons else "Старые полигоны",
               command=other_faces).pack(side="left")
    root.bind("<space>", pause)
    root.bind(".", one_frame)
    root.bind("<Up>", lambda e: sim.set(dino_jump=True))

    status = tk.StringVar()
    note = tk.StringVar()
    r = row()
    ttk.Label(r, textvariable=status, font=("Consolas", 9)).pack(side="left")
    ttk.Label(r, textvariable=note).pack(side="right")

    shown = {"no": -1, "count": 0, "since": time.perf_counter(), "fps": 0.0, "image": None}

    def refresh():
        if sim.frame_no != shown["no"] and sim.frame is not None:
            shown["count"] += sim.frame_no - shown["no"] if shown["no"] >= 0 else 1
            shown["no"] = sim.frame_no
            shown["image"] = ImageTk.PhotoImage(enlarge(sim.frame, scale, mask))
            panel.configure(image=shown["image"])
        elapsed = time.perf_counter() - shown["since"]
        if elapsed >= 0.5:
            shown["fps"] = shown["count"] / elapsed
            shown["count"], shown["since"] = 0, time.perf_counter()
            errors = sim.get("errors")
            status.set(f"{shown['fps']:5.1f} кадр/с   расчёт кадра на ПК {sim.render_ms:4.1f} мс   "
                       f"{sim.get('behavior_state')}   рот {sim.get('mouth_open_level', 0):.2f}   "
                       f"переход {sim.get('emotion_blend', 1):.2f}   "
                       f"показана {sim.get('emotion_shown', '')}   усиление ×{sim.get('auto_gain_now', 1)}"
                       + (f"   ОШИБКА: {errors[-1]}" if errors else ""))
        # the web page may change the same things: keep the window in step
        for var, value in ((link, sim.link), (sound, sim.sound), (speed, sim.clock.speed),
                           (emotion, sim.get("emotion")), (mode, sim.get("mode")),
                           (effect, sim.get("effect")), (party, bool(sim.get("party")))):
            if var.get() != value:
                var.set(value)
        pause_text.set("Дальше" if sim.clock.paused else "Пауза")
        root.after(10, refresh)

    def close():
        sim.stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    sim.start()
    refresh()
    root.mainloop()
    return 0


def headless(args):
    """No window: the firmware and its web page only (open the address in a browser)."""
    sim = Simulator(polygons=args.polygons, boot=args.boot, realtime=True)
    sim.sound = args.sound
    print("веб-интерфейс:", sim.serve(args.web or 8080), "  (Ctrl+C — выход)", flush=True)
    sim.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        sim.stop()
    return 0


def main():
    parser = argparse.ArgumentParser(description="Protogen face simulator")
    parser.add_argument("--web", type=int, default=8080, metavar="PORT",
                        help="port of the web interface (0 = without it)")
    parser.add_argument("--no-window", action="store_true", help="web interface only")
    parser.add_argument("--polygons", action="store_true", help="old polygon faces instead of the artwork")
    parser.add_argument("--boot", action="store_true", help="show the boot animation first")
    parser.add_argument("--scale", type=int, default=7, help="size of one LED in screen pixels")
    parser.add_argument("--sound", choices=SOUNDS, default="speech")
    parser.add_argument("--record", metavar="FILE.gif", help="no window: save a walk through all emotions")
    parser.add_argument("--seconds", type=float, default=14.0, help="length of the recording")
    parser.add_argument("--bench", action="store_true",
                        help="no window: measure the time of one frame on this machine")
    args = parser.parse_args()
    if args.bench:
        if args.seconds == 14.0:
            args.seconds = 20.0
        return bench(args)
    if args.record:
        return record(args)
    return headless(args) if args.no_window else window(args)


if __name__ == "__main__":
    sys.exit(main())
