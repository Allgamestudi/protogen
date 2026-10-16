"""Animation engine building blocks for the protogen face.

  Envelope        - triggerable decaying value (beat zoom/shake/flash/glitch)
  LFO             - slow oscillator (head tilt, idle motion)
  Blinker         - multi-profile blinking: normal / double / sleepy / startled + squint
  Saccade         - realistic gaze: instant jumps + fixation micro-drift + sound reaction
  BeatDetector    - spectral-energy spike detector (kept from v1)
  FeatureExtractor / AudioFeatures - per-frame audio analysis of the 16 FFT bands
  FaceParams      - per-frame overrides that vignettes write into
  Vignettes       - Yawn, LookAround, NoseTwitch, Sneeze (idle mini-scenes)
  Behavior        - top-level FSM: ACTIVE / IDLE / SLEEP (+ PARTY override)
  SpectrogramHistory - rolling history for the waterfall display
"""
import math
import random
import time
from dataclasses import dataclass, field

import numpy as np


def _clamp01(x):
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def _ease_in_out(p):
    return 0.5 - 0.5 * math.cos(math.pi * _clamp01(p))


# primitives
class Envelope:
    """Chargeable envelope: trigger() sets a peak, value decays exponentially."""

    def __init__(self, decay=6.0):
        self.value = 0.0
        self.decay = decay

    def trigger(self, strength=1.0):
        if strength > self.value:
            self.value = min(1.5, strength)

    def update(self, dt, decay=None):
        if decay is not None:
            self.decay = decay
        self.value *= math.exp(-self.decay * max(0.0, dt))
        if self.value < 1e-4:
            self.value = 0.0
        return self.value


class LFO:
    def __init__(self, freq=0.14, amp=1.0, phase=0.0):
        self.freq, self.amp, self.phase = freq, amp, phase

    def value(self, t):
        return math.sin(2.0 * math.pi * self.freq * t + self.phase) * self.amp


# blinking
# Each profile: close/hold fractions of duration (open = remainder), duration sec.
BLINK_PROFILES = {
    "normal":   {"close": 0.30, "hold": 0.25, "dur": 0.22},
    "sleepy":   {"close": 0.40, "hold": 0.35, "dur": 0.60},
    "startled": {"close": 0.10, "hold": 0.25, "dur": 0.16},
}


class Blinker:
    """Multi-profile blinker.

    update() returns eye closed-ness 0..1.  Supports:
      - scheduled blinks (profile depends on mode: normal / sleepy)
      - double blinks with configurable probability
      - trigger_startled() - instant fast blink on loud transients
      - set_squint(level, duration) - partial closure held for a while
      - sleepy mode keeps a half-lidded baseline
    """

    def __init__(self, min_interval=3.0, max_interval=7.0):
        self.min_int = min_interval
        self.max_int = max_interval
        self.mode = "normal"
        self.next_t = time.monotonic() + random.uniform(min_interval, max_interval)
        self.cur = None            # (profile_dict, t0)
        self.pending = None        # profile name queued (double blink)
        self.pending_t = 0.0
        self.squint_level = 0.0
        self.squint_until = 0.0

    def trigger(self, profile="normal"):
        self.cur = (dict(BLINK_PROFILES[profile]), time.monotonic())

    def trigger_startled(self):
        if self.cur is None:           # don't interrupt an ongoing blink
            self.trigger("startled")

    def set_squint(self, level, duration=1.0):
        self.squint_level = _clamp01(level)
        self.squint_until = time.monotonic() + duration

    def update(self, enabled=True, min_int=None, max_int=None,
               duration=None, mode=None, double_prob=0.15):
        if min_int is not None:
            self.min_int = min_int
        if max_int is not None:
            self.max_int = max_int
        if mode is not None:
            self.mode = mode
        now = time.monotonic()
        if not enabled:
            self.cur = None
            self.pending = None
            return 0.0

        # fire queued double-blink
        if self.cur is None and self.pending and now >= self.pending_t:
            self.trigger(self.pending)
            self.pending = None

        # schedule a regular blink
        if self.cur is None and self.pending is None and now >= self.next_t:
            prof_name = "sleepy" if self.mode == "sleepy" else "normal"
            self.trigger(prof_name)
            if prof_name == "normal" and random.random() < double_prob:
                self.pending = "normal"
            self.next_t = now + random.uniform(self.min_int, self.max_int)

        level = 0.0
        if self.cur is not None:
            prof, t0 = self.cur
            dur = prof["dur"]
            if duration is not None and prof is not BLINK_PROFILES["startled"]:
                # web setting scales the normal profile
                dur = duration if self.mode != "sleepy" else prof["dur"]
            t = (now - t0) / max(0.05, dur)
            if t >= 1.0:
                self.cur = None
                if self.pending:
                    self.pending_t = now + 0.13
            else:
                c, h = prof["close"], prof["hold"]
                if t < c:
                    level = _ease_in_out(t / c)
                elif t < c + h:
                    level = 1.0
                else:
                    level = 1.0 - _ease_in_out((t - c - h) / max(1e-3, 1.0 - c - h))

        baseline = 0.30 if self.mode == "sleepy" else 0.0
        squint = self.squint_level if now < self.squint_until else 0.0
        return min(1.0, max(level, baseline, squint))


# gaze (saccades)
class Saccade:
    """Realistic eye movement: rapid jumps (30-60 ms, easeOutExpo), fixations
    with Perlin-ish micro-drift, and reactive glances toward sudden sounds."""

    def __init__(self):
        self.pos = np.zeros(2)
        self.src = np.zeros(2)
        self.dst = np.zeros(2)
        self.jump_t0 = 0.0
        self.jump_dur = 0.045
        self.fix_until = time.monotonic() + random.uniform(0.6, 2.5)
        self.last_react = 0.0

    def _new_target(self, amp):
        r = random.random()
        if r < 0.55:        # near center
            return np.array([random.uniform(-0.4, 0.4), random.uniform(-0.25, 0.25)])
        if r < 0.85:        # horizontal glance
            return np.array([random.choice([-1, 1]) * random.uniform(0.5, 1.0) * amp,
                             random.uniform(-0.25, 0.25)])
        return np.array([random.uniform(-1, 1) * amp,           # diagonal
                         random.uniform(-0.5, 0.5) * amp * 0.5])

    def _start_jump(self, dst, now):
        self.src = self.pos.copy()
        self.dst = np.asarray(dst, dtype=float)
        self.jump_t0 = now
        self.jump_dur = random.uniform(0.03, 0.06)

    def react_to_sound(self, amp):
        """Quick glance to a random side - call on sharp audio onsets."""
        now = time.monotonic()
        if now - self.last_react < 0.6:
            return
        self.last_react = now
        side = random.choice([-1, 1])
        self._start_jump(np.array([side * random.uniform(0.6, 1.0) * amp,
                                   random.uniform(-0.3, 0.3)]), now)
        self.fix_until = now + random.uniform(0.8, 2.0)

    def update(self, enabled=True, amp=2.5, min_fix=0.6, max_fix=2.8):
        now = time.monotonic()
        if not enabled:
            self.pos *= 0.85
            return float(self.pos[0]), float(self.pos[1])

        if now >= self.fix_until and (now - self.jump_t0) > self.jump_dur:
            self._start_jump(self._new_target(amp), now)
            self.fix_until = now + random.uniform(min_fix, max_fix)

        jt = (now - self.jump_t0) / self.jump_dur
        if jt < 1.0:                                    # easeOutExpo jump
            k = 1.0 - 2.0 ** (-10.0 * max(0.0, jt))
            self.pos = self.src + (self.dst - self.src) * k
        else:
            self.pos = self.dst.copy()

        # fixation micro-drift (sum of incommensurate sines ~ noise)
        drift_x = (math.sin(now * 7.3) + math.sin(now * 13.1 + 1.0)) * 0.075
        drift_y = (math.sin(now * 6.1 + 2.0) + math.sin(now * 11.7)) * 0.06
        return float(self.pos[0] + drift_x), float(self.pos[1] + drift_y)


# Backward-compat alias (old renderer imported EyeDart)
EyeDart = Saccade


# audio analysis
class BeatDetector:
    """Spectral-energy spike detector with running baseline + refractory period."""

    def __init__(self, threshold_ratio=1.5, refractory=0.18, abs_floor=0.10):
        self.baseline = 0.0
        self.threshold = threshold_ratio
        self.refractory = refractory
        self.abs_floor = abs_floor
        self.last_beat = 0.0

    def update(self, energy, threshold=None, refractory=None, abs_floor=None):
        if threshold is not None:
            self.threshold = threshold
        if refractory is not None:
            self.refractory = refractory
        if abs_floor is not None:
            self.abs_floor = abs_floor
        now = time.monotonic()
        rate = 0.05 if energy < self.baseline * 1.2 else 0.02
        self.baseline = self.baseline * (1.0 - rate) + energy * rate
        is_beat = (energy > self.abs_floor
                   and energy > self.baseline * self.threshold
                   and now - self.last_beat > self.refractory)
        if is_beat:
            self.last_beat = now
            strength = min(1.0, energy / max(self.baseline, 0.02) / 3.0)
            return True, strength
        return False, 0.0


class BeatFlash:
    """Kept for backward compat - Envelope is the generic replacement."""

    def __init__(self, decay=6.0):
        self.intensity = 0.0
        self.decay = decay

    def trigger(self, strength=1.0):
        self.intensity = max(self.intensity, min(1.0, strength))

    def update(self, dt, decay=None):
        if decay is not None:
            self.decay = decay
        self.intensity = max(0.0, self.intensity - self.decay * dt)
        return self.intensity


@dataclass
class AudioFeatures:
    bass: float = 0.0          # bands 0-2
    speech: float = 0.0        # bands 3-9
    treble: float = 0.0        # bands 10-15
    rms: float = 0.0
    rms_slow: float = 0.0      # ~5 s average
    centroid: float = 0.0      # 0..1 spectral centroid
    beat: float = 0.0          # 0 or beat strength this frame
    onset: float = 0.0         # high-band transient
    silence_time: float = 0.0  # seconds of continuous silence


class FeatureExtractor:
    """Turns the raw 16-band frame into a compact AudioFeatures object."""

    def __init__(self, silence_threshold=0.04):
        self.beat_det = BeatDetector()
        self.prev_treble = 0.0
        self.silence_since = None
        self.silence_threshold = silence_threshold
        self.rms_slow = 0.0
        self.centroid_smooth = 0.0

    def update(self, bars, now, beat_threshold=1.5):
        b = np.asarray(bars, dtype=np.float32)
        n = b.size
        if n == 0:
            return AudioFeatures()
        bass = float(b[0:3].mean()) if n >= 3 else float(b.mean())
        speech = float(b[3:10].mean()) if n >= 10 else float(b.mean())
        treble = float(b[10:].mean()) if n >= 11 else 0.0
        rms = float(b.mean())

        s = float(b.sum())
        if s > 1e-4:
            idx = np.arange(n, dtype=np.float32)
            c = float((idx * b).sum() / s) / max(1, n - 1)
        else:
            c = self.centroid_smooth
        self.centroid_smooth += (c - self.centroid_smooth) * 0.3

        onset = max(0.0, treble - self.prev_treble * 1.6)
        self.prev_treble = treble

        is_beat, strength = self.beat_det.update(bass, threshold=beat_threshold)

        if rms < self.silence_threshold:
            if self.silence_since is None:
                self.silence_since = now
            st = now - self.silence_since
        else:
            self.silence_since = None
            st = 0.0

        self.rms_slow += (rms - self.rms_slow) * 0.01

        return AudioFeatures(bass, speech, treble, rms, self.rms_slow,
                             self.centroid_smooth,
                             strength if is_beat else 0.0, onset, st)


# vignettes
@dataclass
class FaceParams:
    """Per-frame overrides that vignettes / behavior write into."""
    mouth_override: float = None    # direct mouth-open level (yawn)
    blink_floor: float = 0.0        # minimum eye closure
    nose_dy: float = 0.0            # nose vertical nudge
    scale_mod: float = 1.0          # extra zoom multiplier
    dx_mod: float = 0.0
    dy_mod: float = 0.0
    sparkle_burst: bool = False     # one-shot particle burst


class Vignette:
    DURATION = 1.0

    def __init__(self):
        self.t0 = time.monotonic()
        self.done = False

    def t(self):
        t = time.monotonic() - self.t0
        if t >= self.DURATION:
            self.done = True
        return t

    def apply(self, fp: FaceParams):
        raise NotImplementedError


class Yawn(Vignette):
    DURATION = 3.2

    def apply(self, fp):
        t = self.t()
        if self.done:
            return
        if t < 1.2:
            m = _ease_in_out(t / 1.2)
        elif t < 2.0:
            m = 1.0
        else:
            m = 1.0 - _ease_in_out((t - 2.0) / 1.2)
        fp.mouth_override = max(fp.mouth_override or 0.0, m)
        fp.blink_floor = max(fp.blink_floor, 0.55 * m)


class LookAround(Vignette):
    DURATION = 3.6

    def __init__(self, saccade):
        super().__init__()
        self.sac = saccade
        self.steps = [0.0, 0.9, 1.8, 2.8]
        self.dirs = [(-2.5, 0.0), (2.5, -0.5), (-1.5, 0.8), (0.0, 0.0)]
        self.fired = [False] * 4

    def apply(self, fp):
        t = self.t()
        if self.done:
            return
        now = time.monotonic()
        for i, st in enumerate(self.steps):
            if t >= st and not self.fired[i]:
                self.fired[i] = True
                self.sac._start_jump(np.array(self.dirs[i]), now)
                self.sac.fix_until = now + 0.85


class NoseTwitch(Vignette):
    DURATION = 0.5

    def apply(self, fp):
        t = self.t()
        if self.done:
            return
        fp.nose_dy = -abs(math.sin(t / self.DURATION * math.pi * 4.0)) * 1.0


class Sneeze(Vignette):
    DURATION = 1.1

    def apply(self, fp):
        t = self.t()
        if self.done:
            return
        if t < 0.45:                                 # wind-up: squeeze + eyes close
            p = t / 0.45
            fp.scale_mod *= 1.0 - 0.04 * _ease_in_out(p)
            fp.blink_floor = max(fp.blink_floor, p)
        elif t < 0.55:                               # explosion
            fp.scale_mod *= 1.06
            fp.dy_mod += 2.0
            fp.sparkle_burst = True
            fp.blink_floor = max(fp.blink_floor, 1.0)
        else:                                        # damped-spring recovery
            p = (t - 0.55) / 0.55
            fp.dy_mod += 2.0 * math.exp(-4.0 * p) * math.cos(10.0 * p)


# behavior FSM
class Behavior:
    """ACTIVE <-> IDLE -> SLEEP state machine + idle vignette scheduler.

    PARTY is an override driven by the web toggle; ERROR is decided by the
    renderer (it owns connection-age tracking).
    """

    VIGNETTE_WEIGHTS = [(Yawn, 3), (LookAround, 5), (NoseTwitch, 2), (Sneeze, 1)]

    def __init__(self, saccade):
        self.sac = saccade
        self.state = "ACTIVE"
        self.prev_state = "ACTIVE"
        self.vignette = None
        self.next_vignette_t = time.monotonic() + 12.0

    def _spawn_vignette(self):
        total = sum(w for _, w in self.VIGNETTE_WEIGHTS)
        r = random.uniform(0, total)
        for cls, w in self.VIGNETTE_WEIGHTS:
            r -= w
            if r <= 0:
                return cls(self.sac) if cls is LookAround else cls()
        return Yawn()

    def update(self, feat: AudioFeatures, now,
               party=False, idle_after=20.0, sleep_after=150.0,
               vignettes_enabled=True):
        self.prev_state = self.state

        if party:
            self.state = "PARTY"
            self.vignette = None
            return self.state
        if self.state == "PARTY":
            self.state = "ACTIVE"

        if self.state == "ACTIVE":
            if feat.silence_time > idle_after:
                self.state = "IDLE"
                self.next_vignette_t = now + random.uniform(4.0, 10.0)
        elif self.state == "IDLE":
            if feat.silence_time == 0.0:
                self.state = "ACTIVE"
                self.vignette = None
            elif feat.silence_time > sleep_after:
                self.state = "SLEEP"
                self.vignette = None
        elif self.state == "SLEEP":
            if feat.silence_time == 0.0:
                self.state = "ACTIVE"     # renderer fires the startled blink

        if self.state == "IDLE" and vignettes_enabled:
            if self.vignette is not None and self.vignette.done:
                self.vignette = None
                self.next_vignette_t = now + random.uniform(8.0, 20.0)
            elif self.vignette is None and now >= self.next_vignette_t:
                self.vignette = self._spawn_vignette()
        elif self.vignette is not None and self.vignette.done:
            self.vignette = None

        return self.state


# spectrogram
class SpectrogramHistory:
    """Rolling history of bar values for waterfall display."""

    def __init__(self, n_bars, n_frames):
        self.n_bars = n_bars
        self.n_frames = n_frames
        self.buf = np.zeros((n_frames, n_bars), dtype=np.float32)
        self.head = 0

    def push(self, bars):
        self.buf[self.head] = bars
        self.head = (self.head + 1) % self.n_frames

    def as_image(self):
        return np.roll(self.buf, -self.head, axis=0)[::-1]
