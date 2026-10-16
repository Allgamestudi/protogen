"""Process-global state shared between threads + JSON persistence of settings."""
import json
import os
import threading
import time
from collections import deque
import numpy as np

import config

lock = threading.Lock()
stop_event = threading.Event()

# ---- Settings (user-tunable, persisted to disk) ----
DEFAULT_SETTINGS = {
    # Lip-sync (matrix_renderer)
    "voice_band_lo": 2,
    "voice_band_hi": 10,
    "voice_gain": 3.0,
    "voice_threshold": 0.05,
    "lipsync_attack_open": 0.55,
    "lipsync_attack_close": 0.20,
    # Breathing motion
    "breath_amp_x": 1.0,
    "breath_amp_y": 1.0,
    "breath_speed": 1.0,
    # EQ rendering
    "eq_attack": 0.95,
    "eq_decay": 0.45,
    "eq_peak_gravity": 2.2,
    # Boot animation timings (applied on next service restart)
    "boot_console_line_time": 0.35,
    "boot_loadbar_duration": 1.4,
    "boot_glitch_duration": 1.4,
    # Rainbow effect speed
    "rainbow_speed": 40.0,
    # Emotion transition
    "emotion_transition_speed": 0.08,
    # Blink
    "blink_enabled": True,
    "blink_min_interval": 3.0,
    "blink_max_interval": 7.0,
    "blink_duration": 0.22,
    "blink_double_prob": 0.15,
    "startled_enabled": True,
    # Saccades (eye movement)
    "saccade_enabled": True,
    "saccade_amp": 2.5,
    "saccade_min_fix": 0.6,
    "saccade_max_fix": 2.8,
    "saccade_sound_react": True,
    # Beat detection / FX
    "beat_enabled": True,
    "beat_threshold": 1.5,
    "beat_flash_decay": 6.0,
    "beat_flash_strength": 0.35,
    "beat_zoom_amp": 0.06,
    "beat_shake_amp": 1.5,
    "beat_mouth_kick": 0.3,
    # Aura outline
    "aura_enabled": True,
    "aura_intensity": 0.8,
    # Motion extras
    "breath_scale_amp": 1.0,
    "tilt_enabled": True,
    "tilt_amp": 1.5,
    # IMU vestibulo-ocular: eyes drift opposite head ROTATION (gyro-driven)
    "imu_eye_enabled": True,
    "imu_eye_amp": 0.08,         # pixels per (deg/s) of head rotation
    "imu_eye_stiffness": 30.0,   # spring stiffness - higher = eyes track tighter
    "imu_eye_damping": 6.0,      # damping - lower = more swimming overshoot
    # IMU step bob: detect impulse spikes on accel magnitude -> bob face down
    "imu_bob_enabled": True,
    "imu_bob_amp": 4.0,          # impulse magnitude - velocity kick per g of spike
    "imu_bob_threshold": 0.12,   # min |a| deviation from 1 g baseline to count as a step
    "imu_bob_refractory": 0.22,  # min seconds between detected steps
    "imu_bob_stiffness": 110.0,  # restoring spring - higher = quicker return to rest
    "imu_bob_damping": 3.5,      # bob damping - lower = more bouncing
    # Dizzy effect: sustained shake -> spinning spirals in eyes + open mouth
    "imu_dizzy_enabled": True,
    "imu_dizzy_trigger_s": 2.0,        # seconds of continuous shake required to trigger
    "imu_dizzy_duration_s": 3.0,       # how long the dizzy state lasts after triggering
    "imu_dizzy_spin_rate": 8.0,        # spiral rotation speed, radians/s
    "imu_dizzy_shake_threshold": 0.12, # smoothed |a|-1 g above this counts as shaking
    "imu_dizzy_radius": 10.0,          # spiral max radius, px
    "imu_dizzy_turns": 3.0,            # how many turns the spiral makes
    # IMU OUCH: brief startled override + envelopes on impact spike
    "imu_ouch_enabled": True,
    "imu_ouch_duration_s": 0.8,        # how long the surprised override lasts after an impact
    # IMU freefall: hold surprised + wide-open mouth while falling
    "imu_freefall_enabled": True,
    # Catchlight (white highlight in the eye for liveliness)
    "pupil_gaze": True,                # eyes with a pupil look around with the pupil, not the whole eye
    "pupil_gaze_gain": 1.3,            # how far the pupil travels compared with the eye darts
    "eye_style": 0,                    # 0 = stylised (original artwork), 1 = anatomical (lids and pupil)
    "catchlight_enabled": True,
    "liveliness": 1.0,                 # character motion of each emotion: 0 = off, 2 = exaggerated
    "emotion_pop": 0.6,                # zoom kick when the emotion changes
    "eyes_parallel": True,             # both eyes look the same way (the 2nd panel is a mirror)
    "asymmetry_enabled": True,         # winks and doubtful looks: one eye differs for a moment
    "visemes_enabled": True,           # mouth shape follows the sound: round on "o", flat on "s"
    # Reactions (pi/reactions.py)
    "auto_gain": True,                 # calibrate the sound level to the surroundings all the time
    "auto_gain_min_peak": 0.06,        # quieter peaks than this are noise and are not amplified
    "auto_emotion": True,              # bang -> scared, music -> happy, long silence -> sleepy
    "auto_sleepy_after_s": 45.0,
    "gestures_enabled": True,          # nod, head shake, petting, poke, double knock
    "catchlight_double": True,         # two-pixel-wide catchlight
    "catchlight_trail": 0.35,          # 0 = stays put, 1 = follows eye fully (kills the effect)
    # Mouth EQ: small spectrum bars rendered inside the open mouth
    "mouth_eq_enabled": True,
    "mouth_eq_threshold": 0.5,         # mouth must be at least this open for bars to show
    # Post FX
    "fx_scanline": 0.4,
    "fx_noise": 0.15,
    "fx_rgbsplit_beat": True,
    "fx_bloom": 0.0,
    # Protection of the power supply: the most a frame may draw, as a share of
    # "all LEDs white at 100 %". Brighter frames are dimmed to this. 1.0 = no limit.
    "power_limit": 0.25,
    # Idle / sleep behavior
    "idle_after_s": 20.0,
    "sleep_after_s": 150.0,
    "vignettes_enabled": True,
    "sparkle_enabled": True,
    # Proximity (VL53L0X ToF rangefinder)
    "proximity_enabled": True,
    "proximity_near_mm": 100,      # rude-close (squint, look away)
    "proximity_close_mm": 400,     # personal-zone (track user, brighter)
    "proximity_far_mm": 1500,      # beyond this - accelerate idle/sleep
    "proximity_gesture_mm_s": 800.0,   # approach speed -> startled
    "proximity_brightness_boost": True,
    "proximity_wake_enabled": True,    # quick approach wakes from SLEEP
    "proximity_idle_factor": 0.4,      # far-away IDLE happens this fraction faster
    "proximity_smoothing": 0.35,       # EMA factor for displayed distance
    "proximity_min_valid_mm": 50,      # below this distance reading is treated as
                                        # crosstalk noise (sensor reflecting off visor)
    "proximity_emotion_near":  "angry",  # emotion when distance < near_mm
    "proximity_emotion_close": "happy",  # emotion when distance < close_mm
    # Proximity recoil - face physically jerks back when something gets too close
    "proximity_recoil_amp":       60.0,  # initial velocity kick (px/s) on close->near
    "proximity_recoil_stiffness": 70.0,  # spring constant - higher = faster return
    "proximity_recoil_damping":    6.0,  # damping - lower = more bouncing back
    "proximity_eye_widen":        0.18,  # +18% eye size in close zone (curiosity)
    # Text mode
    "text_speed": 24.0,
    "text_scale": 1,                 # 1=4x6, 2=8x12, 3=12x18 (pixel multiplier)
    "text_effect": "solid",          # "solid"/"rainbow"/"blink"/"color_cycle"
    # Colour music (mode "music"): "auto"/"zones"/"rings"/"plasma"/"flash"
    "music_style": "auto",
    # Spectrogram
    "spec_gain": 1.4,
    # Dino game
    "dino_x":           24,          # dino sprite X position (0..panel_width)
    "dino_jump_v":      92.0,        # initial jump velocity (px/s)
    "dino_gravity":     320.0,       # gravity (px/s²)
    "dino_speed_base":  32.0,        # initial scroll speed (px/s)
    "dino_speed_ramp":  0.0009,      # speed ramp per pixel distance
    "dino_spawn_min_s": 0.7,         # min seconds between obstacles (scaled by speed)
    "dino_spawn_max_s": 1.6,         # max seconds between obstacles (scaled by speed)
    "dino_tof_enabled": True,        # jump when hand passes near ToF sensor
    "dino_tof_lo_mm":   50,          # distance below this -> trigger jump (armed)
    "dino_tof_hi_mm":   150,         # distance above this -> re-arm
}

# All persistent files (settings, UI state, optional logs) live in protogen/saves/
# so they're easy to back up, sync, or version-control as a unit.
_PI_DIR    = os.path.dirname(os.path.abspath(__file__))           # .../protogen/pi/
SAVES_DIR  = os.path.join(os.path.dirname(_PI_DIR), "saves")      # .../protogen/saves/
try:
    os.makedirs(SAVES_DIR, exist_ok=True)
except Exception:
    pass

SETTINGS_FILE = os.path.join(SAVES_DIR, "settings.json")


# ---- UI / device state ----
state = {
    "mode": config.DEFAULTS["mode"],
    "emotion": config.DEFAULTS["emotion"],
    "effect": config.DEFAULTS["effect"],
    "color": list(config.DEFAULTS["color"]),
    "brightness": config.DEFAULTS["brightness"],
    "rotate_180": config.DEFAULTS["rotate_180"],
    "mirror": config.DEFAULTS["mirror"],
    "has_image": config.DEFAULTS["has_image"],
    "gradient_colors": config.DEFAULTS.get("gradient_colors", [[255, 0, 0], [0, 0, 255]]),
    "gradient_angle":  config.DEFAULTS.get("gradient_angle", 0),
    "mouth_open_level": 0.0,
    # Text scroller content
    "text_content": "PROTOGEN ONLINE",
    "text_color": [0, 220, 70],
    # GIF playback (live)
    "gif_frame_count": 0,
    # Animations live state
    "is_blinking": False,
    "beat_flash": 0.0,
    "behavior_state": "ACTIVE",
    # Party mode override (web toggle)
    "party": False,
    # Per-emotion remembered colors: {emotion_id: [r, g, b]}
    "emotion_colors": {},
    # Emotion transition
    "emotion_from": config.DEFAULTS["emotion"],
    "emotion_blend": 1.0,          # 0.0 = from, 1.0 = to (= current emotion)
    # Dino game (mode="dino"): renderer pops `dino_jump` each frame
    "dino_jump": False,
    "dino_score": 0,
    "dino_high": 0,
    # Connection
    "connected": False,
    "port": None,
    "reconnects": 0,
    "boot_count": 0,
    # EQ data
    "bars": [0] * config.BANDS,
    "last_eq_ms": 0,
    "eq_rate_hz": 0.0,
    # ESP stats
    "esp_uptime_s": 0,
    "esp_free_heap": 0,
    "esp_notify_rate": 0,
    "esp_sample_rate": 0,
    "esp_peak_rms": 0,
    "esp_i2s_ok": False,
    "esp_calibrated": False,
    "esp_tof_ok": False,
    "esp_acc_ok": False,
    # IMU (QMI8658 on Waveshare ESP32-S3-LCD-1.46)
    "accel_mg": [0, 0, 0],         # [ax, ay, az] in milli-g
    "gyro_dps": [0, 0, 0],         # [gx, gy, gz] angular rate in deg/s
    "tilt_deg": 0,                 # angle between board normal and gravity
    "motion_shake": False,
    "motion_impact": False,        # latched until consumed by renderer
    "motion_impact_ms": 0,         # when set - auto-decays after 1 s
    "motion_freefall": False,
    "motion_flat": False,
    "last_motion_ms": 0,
    # VL53L0X distance (mm). None when out of range or not connected.
    "distance_mm": None,
    "distance_valid": False,
    "distance_smoothed_mm": None,
    "distance_zone": "unknown",   # near / close / mid / far / unknown
    "last_distance_ms": 0,
    # ESP settings (mirrored from device)
    "noise_margin": config.DEFAULTS["noise_margin"],
    "db_gain": config.DEFAULTS["db_gain"],
    "amp_mult": config.DEFAULTS["amp_mult"],
    "smoothing": config.DEFAULTS["smoothing"],
    # Persisted user settings
    "settings": dict(DEFAULT_SETTINGS),
    # Telemetry
    "errors": [],
}

# Live data structures
target_bars = np.zeros(config.BANDS, dtype=np.float32)
uploaded_image = np.zeros((config.H, config.W, 3), dtype=np.uint8)
# GIF playback: list of (frame_array, delay_seconds)
uploaded_gif_frames = []
eq_times = deque(maxlen=120)
ser_handle = {"ser": None}
# The frame the renderer sent to the panels last (H x W*chain x 3, uint8) and
# its number. The web page shows it as a live preview.
preview = {"frame": None, "seq": 0}


def log_err(msg: str) -> None:
    with lock:
        state["errors"].append(f"{time.strftime('%H:%M:%S')} {msg}")
        state["errors"] = state["errors"][-8:]


# ---- Settings persistence ----
def save_settings() -> bool:
    """Persist current settings to disk (atomically). Returns True on success."""
    with lock:
        snapshot = dict(state["settings"])
    tmp = SETTINGS_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(snapshot, f, indent=2)
        os.replace(tmp, SETTINGS_FILE)
        return True
    except Exception as e:
        log_err(f"save_settings → {SETTINGS_FILE}: {e}")
        return False


def load_settings() -> None:
    """Read saved settings from disk and merge into state."""
    if not os.path.exists(SETTINGS_FILE) or os.path.getsize(SETTINGS_FILE) == 0:
        return                              # nothing saved yet (or an empty file after a power cut)
    try:
        with open(SETTINGS_FILE) as f:
            loaded = json.load(f)
        with lock:
            # only keep known keys
            for k, default in DEFAULT_SETTINGS.items():
                if k in loaded:
                    try:
                        # cast to type of default
                        if isinstance(default, bool):
                            state["settings"][k] = bool(loaded[k])
                        elif isinstance(default, int):
                            state["settings"][k] = int(loaded[k])
                        elif isinstance(default, float):
                            state["settings"][k] = float(loaded[k])
                        else:
                            state["settings"][k] = loaded[k]
                    except Exception:
                        pass
    except Exception as e:
        log_err(f"load_settings: {e}")


def update_setting(key: str, value) -> bool:
    """Set a single setting if its key is known. Returns True on success."""
    if key not in DEFAULT_SETTINGS:
        return False
    default = DEFAULT_SETTINGS[key]
    try:
        if isinstance(default, bool):
            value = bool(value)
        elif isinstance(default, int):
            value = int(value)
        elif isinstance(default, float):
            value = float(value)
    except (TypeError, ValueError):
        return False
    with lock:
        state["settings"][key] = value
    return True


def reset_settings() -> None:
    """Restore all settings to defaults."""
    with lock:
        state["settings"] = dict(DEFAULT_SETTINGS)
    save_settings()


# ---- UI state persistence (mode/emotion/colors/etc.) ----
UI_STATE_FILE = os.path.join(SAVES_DIR, "ui_state.json")

# What survives a restart. "party" and live telemetry intentionally excluded.
UI_KEYS = [
    "mode", "emotion", "effect", "color",
    "brightness", "rotate_180", "mirror",
    "gradient_colors", "gradient_angle",
    "text_content", "text_color",
    "emotion_colors",
]


def save_ui_state() -> bool:
    with lock:
        snapshot = {k: state[k] for k in UI_KEYS if k in state}
    tmp = UI_STATE_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(snapshot, f, indent=2)
        os.replace(tmp, UI_STATE_FILE)
        return True
    except Exception as e:
        log_err(f"save_ui_state → {UI_STATE_FILE}: {e}")
        return False


def load_ui_state() -> None:
    if not os.path.exists(UI_STATE_FILE):
        return
    try:
        with open(UI_STATE_FILE) as f:
            loaded = json.load(f)
    except Exception as e:
        log_err(f"load_ui_state: {e}")
        return
    with lock:
        for k in UI_KEYS:
            if k in loaded:
                state[k] = loaded[k]
        # sanity: uploaded images don't survive restarts
        if state.get("mode") == "image":
            state["mode"] = "emotion"
        if not isinstance(state.get("emotion_colors"), dict):
            state["emotion_colors"] = {}
        if not (isinstance(state.get("color"), list) and len(state["color"]) == 3):
            state["color"] = list(config.DEFAULTS["color"])


# ---- dirty-flag flusher: batches frequent UI writes (protects the SD card) ----
_dirty = {"ui": False}


def mark_ui_dirty() -> None:
    _dirty["ui"] = True


def flush_all() -> None:
    """Write any pending state to disk immediately (call on shutdown)."""
    if _dirty.get("ui"):
        _dirty["ui"] = False
        save_ui_state()


def _flusher():
    while not stop_event.is_set():
        time.sleep(2.0)
        if _dirty.get("ui"):
            _dirty["ui"] = False
            save_ui_state()


# Load on import, then start the background flusher
load_settings()
load_ui_state()
threading.Thread(target=_flusher, daemon=True, name="state-flusher").start()
