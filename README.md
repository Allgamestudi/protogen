# Protogen — robot face on LED matrix

A modular controller for a 64×32 HUB75 LED matrix, designed as a protogen-style robot face. Runs on Raspberry Pi 3 B+, uses an ESP32-C3 with an INMP441 microphone for audio reactivity, and exposes a web UI for emotion / image / equalizer control.

## Stack

| Component | Role |
|---|---|
| **Raspberry Pi 3 B+** | drives the HUB75 64×32 panel via [hzeller/rpi-rgb-led-matrix](https://github.com/hzeller/rpi-rgb-led-matrix), runs the web UI and matrix renderer |
| **ESP32-C3 Super Mini** | samples INMP441 over I²S, runs FFT, sends 16-band magnitudes to the Pi over USB CDC |
| **INMP441** | digital MEMS microphone |
| **HUB75 64×32 P3** | the face |

## Quick start

```bash
# clone
git clone <repo> ~/protogen
cd ~/protogen

# install Pi-side deps
./scripts/install.sh

# flash ESP32 firmware (Arduino CLI required)
./scripts/flash_esp.sh

# start app
./scripts/start.sh

# open web UI
# http://<pi-ip>:5001
```

Default mode on startup: **emotion = angry**, color = red, mirror = ON.

## Repo layout

```
firmware/        ESP32-C3 PlatformIO project
pi/              Python application for the Pi
  ├─ main.py       entry point
  ├─ state.py      shared state + lock
  ├─ emotions.py   emotion table, polygons, face API
  ├─ emotion_art.py  faces from the artist's PNGs (assets/emotion_masks_64x32)
  ├─ serial_proto.py  ESP32 USB packet parser
  ├─ matrix_renderer.py  matrix render thread
  ├─ web_app.py    Flask web UI
  ├─ image_convert.py  uploaded picture / GIF -> 64x32 frames
  └─ updater.py    update from git, used by the web UI
assets/          PNG / SVG / GIF media
scripts/         install / start / flash helpers + systemd unit
docs/            wiring diagram, serial protocol
```

## Address

The device answers at **http://proto.local** on every network it joins, and
nobody has to set that up: at every start the service checks that the hostname is
`proto` and that avahi (which announces `proto.local`) is running, and puts both
right if not (`pi/hostname_guard.py`, name in `config.HOSTNAME`). The System tab
of the page shows the address.

To give one device another name: `sudo bash scripts/setup_hostname.sh myname`
(remembered in `saves/hostname`, survives updates).

The web UI listens on port 80 and on the old port 5001. Windows needs a current
Windows 10/11 (or Bonjour) to resolve `.local` names; Android resolves them from
version 12. Two devices in one network: the second one becomes `proto-2.local`.

## Updates from the web page

*Advanced settings → Software update* checks the git repository the device
was installed from, lists what is new and installs it. Settings in `saves/`
are not touched. If the new version does not start within a minute, the
previous one is put back automatically.

One-time setup:

1. Publish the project (on the development machine, once):

   ```bash
   git add -A
   git commit -m "Initial public version"
   git remote add origin https://github.com/<user>/<repo>.git
   git push -u origin main
   ```

   The repository must be public, otherwise the devices cannot download from
   it without a password. `.gitignore` keeps the 3D models, the PCB files and
   the settings out of it.

2. On every device that was installed by copying files, once:

   ```bash
   sudo apt-get install -y git
   bash scripts/enable_updates.sh https://github.com/<user>/<repo>.git
   sudo systemctl restart protogen
   ```

   New devices can simply be installed with `git clone`.

After that, releasing an update is `git push`; users press *Check for updates*.

What an update does not do: it does not rebuild the compiled extras (DOOM, the
Bad Apple player, the LED matrix library) and it does not flash the ESP32.

## Web modes

- **😠 Emotion** — render a polygon-based emotion. Solid color or animated rainbow.
- **🖼️ Image** — upload a PNG, JPEG, GIF, WebP or BMP, still or animated. It is scaled to fit
  64×32 with its proportions kept; transparent areas are black; small pixel art is enlarged
  with hard pixels. Animations longer than 600 frames are cut.
- **🎵 EQ** — 16-band equalizer driven by ESP32 microphone.

Display settings (brightness, mirror, rotate) are global.

## Emotion artwork

Six emotions (angry, bad, happy, sad, scared, surprised) are shown from the
artist's 64×32 PNGs in `assets/emotion_masks_64x32/`: at rest the panel shows
those pixels exactly. Lip-sync, blinking and the change between emotions are
computed from the same PNGs (`pi/emotion_art.py`). To change a face, replace
its PNGs and restart.

Sleepy, love and evil have no artwork and are still drawn from the polygons in
`pi/emotions.py`. `EMOTION_ART = False` in `pi/config.py` switches everything
back to polygons.

## Rendering checks

The rendering regression tests run without Raspberry Pi hardware or background
services. Install the NumPy and Pillow dependencies, then run from the project root:

```bash
python -B -m unittest discover -s tests -v
```

For repeatable CPU rendering measurements (also runnable on the Pi):

```bash
python -B scripts/benchmark_rendering.py --output benchmarks/rendering.json
# Compare against a directory containing the original emotions.py and matrix_renderer.py:
python -B scripts/benchmark_rendering.py --baseline /path/to/original/pi --output benchmarks/rendering.json
```

The benchmark runs the actual render loop with deterministic synthetic audio and
a dummy matrix. It writes JSON and Markdown reports with median/p95 frame times
and checks pixel equality when a baseline is supplied. Hardware transfer, VSync,
frame pacing, boot and concurrent web/serial work are excluded; the results are
not physical display FPS or total device CPU usage.

## License

MIT.

## Simulator

`python scripts/simulator.py` runs the real render loop (`pi/matrix_renderer.py`)
on a PC and shows both panels in a window; only the LED matrix and the sound
from the ESP are replaced. Needs NumPy and Pillow.

- emotions (keys 1–9), mode, effect, colour, party, picture/GIF through the same converter as the web page
- sound: silence, speech, music or a manual level, to see lip-sync and beat reactions
- "живость": the `liveliness` setting (also in the web settings): how strongly each emotion moves in its own way (`pi/liveliness.py`), 0 = off
- slow motion ×0.25 / ×0.1, pause (Space) and frame by frame (`.`): the frames are the ones the panel would get
- "ESP на связи" off shows the ERROR screen; `--boot` shows the boot animation; `--polygons` the old faces
- `--record out.gif` saves a walk through all emotions without opening a window

The frame time shown is that of the PC, not of the Raspberry Pi.

## Reactions and artwork extras

- **Artwork for all nine.** `python scripts/make_extra_art.py` draws what the original set lacks: eyes for
  sleepy / love / evil and a closed-eye lid line (`<emotion>_blink_eye.png`) for every emotion. The artist's
  files are never overwritten; any generated PNG can be redrawn by hand (`--force` only redraws its own files).
- **Blink** ends on the drawn lid line; **each emotion arrives its own way** (`TRANSITION_STYLE` in `pi/emotions.py`).
- **Mouth shapes**: round on low sounds, flat on hissing ones (`visemes_enabled`).
- **Winks**: one eye differs for a moment, by itself or from the 😉 buttons (`asymmetry_enabled`).
- **Auto volume** (`auto_gain`): the sound level is calibrated to the surroundings all the time.
- **Emotion from sound** (`auto_emotion`): bang after quiet = scared, music with a beat = happy,
  long silence = sleepy. Picking an emotion by hand pauses it for 8 s.
- **Gestures** (`gestures_enabled`): nod = happy, head shake = bad, hand held at the face = love,
  quick poke = sneeze, two knocks = angry. Needs the IMU / distance sensor.
- **Own emotions from the web page** (Emotion section, "Своя эмоция"): PNG 64×32, light on dark; eye and
  closed mouth are required. Stored in `saves/emotions/`, so updates do not touch them.

All switches are in the web settings, group "Reactions". Logic lives in `pi/reactions.py`
(tests: `tests/test_reactions.py`); the simulator has buttons for every gesture.
- **Eye style** (`eye_style`, group "Eye details"): 0 = stylised, the original flat artwork;
  1 = anatomical, an eyeball with lids and a pupil for every emotion (`<emotion>_real_eye.png`,
  drawn by `scripts/make_extra_art.py`, redraw by hand if you like). Mouth, nose and blink are shared.

## Installing a release by hand

For a Pi that is not set up for updates from the web page yet. `dist/` holds the archive and the
installer (both are built, not kept in git).

    scp dist/protogen-pi-<version>.tar.gz dist/install_release.sh wild@ender.local:/home/wild/
    ssh wild@ender.local "bash install_release.sh protogen-pi-<version>.tar.gz"

The installer first tests the new version next to the running one (unit tests and
`simulator.py --bench`, the frame time on that board; the panel is not touched), then makes a
backup in `saves/`, copies the files, restarts the service and waits for the web page. If the
service does not come up, the backup is put back. `saves/` (settings, own emotions) is kept.
To check a board without installing anything: `python3 scripts/simulator.py --bench`.

## Power limit

A photo or a white screen lights every LED and draws many times the current of a face; a weak
supply then sags and the Pi reboots (`vcgencmd get_throttled` shows `0x50000`). The renderer dims
any frame above `power_limit` (web settings, group "Power"; default 0.25 of "all white at 100 %").
Faces are far below the limit and are not changed.
- **Pupils that look around** (eye style 1): the eyeball (`*_real_ball.png`) and the pupil
  (`*_real_pupil.png`) are separate pictures; eye darts move the pupil under the lids instead of
  the whole eye, and both eyes look the same way (`pupil_gaze`, `pupil_gaze_gain`). `FORWARD` in
  `scripts/make_extra_art.py` is how far the resting pupil sits towards the nose.
- **Boot animation**: power-on line across both panels, boot log with a progress read-out (no
  longer mirrored on the second panel), rain, a beam that traces and fills the face, ignition
  with shock rings. Stages and durations are at the end of `pi/boot_animation.py`.

## Web interface

`pi/webui.html` is the page (one file, no build step); the previous page stays at `/classic`.
English and Russian, switched in the corner and remembered by the browser. On top is a live
copy of the panels (`/api/preview.png`, one pixel per LED). Tabs: Face, Colour, Show, Ears,
Settings, System. Every setting is described in `pi/ui_schema.py` (name and explanation in both
languages, range, default); tapping its name on the page opens the explanation and a reset link.
To add a setting: put the default into `state.DEFAULT_SETTINGS` and an entry into `ui_schema.py`
(`tests/test_web.py` fails if a language is missing).

The simulator serves the same page: `python scripts/simulator.py` prints the address
(`http://127.0.0.1:8080/`, `--web PORT`, `--web 0` to switch off, `--no-window` for the page
alone). It adds a Simulator tab: sound, gestures, time, sensor link. Flask is not needed on the
PC (`pi/miniflask.py` stands in); restart, reboot and updates are switched off there.

## Colour music

Mode `music` (button "Colour music" on the Show tab) turns the panels into a light show:
`pi/colormusic.py`, styles `zones`, `rings`, `plasma`, `flash` and `auto` (setting `music_style`).
It uses the same band levels and beat as the face; the power limit still applies.
Tests: `tests/test_colormusic.py`.

## Wi-Fi from the page

System tab, card "Wi-Fi": the state of the network, a list of networks nearby, name and
password, Connect. `pi/wifi.py` does it through NetworkManager (`nmcli`); the password goes to
nmcli only and is never logged or sent back. A failed attempt removes its profile and returns to
the previous network. "Check for updates" and "Install" first look at the network and answer
"Connect Wi-Fi" (no network) or "No access to the internet" (network, but the update server
cannot be reached). In the simulator the network is pretended: Simulator tab, "Network".
Tests: `tests/test_wifi.py`.
