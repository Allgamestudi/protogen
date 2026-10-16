"""All tunable parameters in one place."""
import os

# Display
W, H        = 64, 32
BRIGHTNESS  = 100
ROTATE_180  = False
MIRROR_LR   = True                # True  -> chain 2 panels: [original | mirrored]
                                   # False -> single panel, no duplication
TARGET_FPS  = 30                  # lowered from 60 to ease CPU load on Pi 4
EMOTION_ART = True                # True: faces from the artist's PNGs in assets/emotion_masks_64x32
                                   # False: the older simplified polygons

# Matrix hardware mapping (rpi-rgb-led-matrix options)
HW_MAPPING    = "regular"
GPIO_SLOWDOWN = 2
PWM_BITS      = 11
CHAIN_LENGTH  = 2 if MIRROR_LR else 1   # driven by MIRROR_LR

# Serial / ESP32
BAUD            = 115200
ESP_USB_VID     = 0x303a         # Espressif
SERIAL_FALLBACK = "/dev/ttyACM0"

# Equalizer
BANDS       = 16
ATTACK      = 0.95               # how fast bars rise
DECAY       = 0.45               # how fast bars fall
PEAK_GRAV   = 2.2                # peak-hold drop acceleration

# Web
WEB_PORT    = 5001
WEB_PORT_PLAIN = 80              # second port, for http://proto.local without ":5001"; 0 = off
HOSTNAME    = "proto"            # the device is kept at http://<this>.local; "" = leave the system alone
UPLOAD_DIR  = "/tmp/led_uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Defaults sent back to UI before ESP reports its own values
DEFAULTS = {
    "mode": "emotion",
    "emotion": "angry",
    "effect": "solid",
    "color": [255, 30, 30],
    "brightness": BRIGHTNESS,
    "rotate_180": ROTATE_180,
    "mirror": MIRROR_LR,
    "has_image": False,
    "gradient_colors": [[255, 0, 0], [0, 0, 255]],   # default: red->blue
    "gradient_angle":  0,                              # degrees: 0=left->right
    "noise_margin": 2.0,
    "db_gain": 20.0,
    "amp_mult": 1,
    "smoothing": 10,
}
