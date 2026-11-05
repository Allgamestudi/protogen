"""Things the face does by itself: auto gain, emotion from sound, gestures, winks.

Fed once per frame by the renderer. No hardware or shared state here, so
they're easy to test (tests/test_reactions.py).
"""
from collections import deque
import math
import random

import numpy as np


# loudness
class AutoGain:
    """Calibrates the sound level all the time.

    The same voice gives small bars in a quiet room and is drowned by the hall
    at a convention. This keeps two running measures per moment: the noise
    floor of every band (what is there all the time: fans, crowd hum) and the
    loudest recent peak above it. The bars are stretched so that the floor is 0
    and the peak is 1; lip-sync, beat detection and the equaliser then behave
    the same everywhere, without touching the gain by hand.
    """

    FLOOR_UP_S = 25.0       # the floor creeps up this slowly (steady sound becomes "floor")
    FLOOR_DOWN_S = 0.6      # and drops quickly when it gets quieter
    PEAK_UP_S = 0.12
    PEAK_HALF_LIFE_S = 9.0  # after a loud moment the scale comes back in about this time

    def __init__(self, bands):
        self.floor = np.zeros(bands, dtype=np.float32)
        self.peak = 0.25
        self.started = False

    def process(self, bars, dt, min_peak=0.06, headroom=0.85):
        """Returns the calibrated bars (0..1).

        min_peak  peaks below this are noise: they are not blown up to full scale
        headroom  the recent peak lands at 1/headroom, so normal speech opens
                  the mouth fully and only shouting clips
        """
        b = np.asarray(bars, dtype=np.float32)
        dt = min(max(dt, 1e-3), 0.25)
        if not self.started:
            self.floor[:] = b
            self.started = True
        up = b > self.floor
        self.floor += (b - self.floor) * np.where(up, dt / self.FLOOR_UP_S, dt / self.FLOOR_DOWN_S)
        level = np.maximum(b - self.floor * 1.15 - 0.01, 0.0)
        top = float(level.max()) if level.size else 0.0
        if top > self.peak:
            self.peak += (top - self.peak) * min(1.0, dt / self.PEAK_UP_S)
        else:
            self.peak *= 0.5 ** (dt / self.PEAK_HALF_LIFE_S)
        self.peak = max(self.peak, min_peak)
        return np.clip(level / (self.peak * headroom), 0.0, 1.0).astype(np.float32)

    @property
    def gain(self):
        """How many times the sound is amplified at the moment."""
        return 1.0 / (self.peak * 0.85)


# mouth shapes
class Visemes:
    """Chooses the shape of the open mouth from the colour of the sound:
    low, hollow sounds ("o", "u") make it round, hissing ones ("s", "sh") flat."""

    def __init__(self):
        self.round = 0.0
        self.wide = 0.0

    def update(self, bars, rate=0.35):
        b = np.asarray(bars, dtype=np.float32)
        n = b.size
        low = float(b[1:max(2, n // 4)].mean())
        mid = float(b[n // 4:n // 2 + 1].mean())
        high = float(b[n // 2 + 2:].mean()) if n // 2 + 2 < n else 0.0
        total = low + mid + high
        want_round = want_wide = 0.0
        if total > 0.08:
            want_round = min(1.0, max(0.0, (low / total - 0.45) * 3.0))
            want_wide = min(1.0, max(0.0, (high / total - 0.35) * 3.0))
        self.round += (want_round - self.round) * rate
        self.wide += (want_wide - self.wide) * rate
        return self.round, self.wide


# emotion from sound
class AutoEmotion:
    """Picks an emotion from what is heard; None = show the one the user chose.

      a sudden loud sound after quiet   -> scared for a moment
      music with a steady beat          -> happy while it plays
      a long silence                    -> sleepy until there is sound again

    After the user picks an emotion by hand it is left alone for a while.
    """

    def __init__(self):
        self.current = None
        self.until = 0.0
        self.beats = []
        self.base = None
        self.hands_off_until = 0.0
        self.prev_rms = 0.0
        self.quiet_since = None

    def update(self, feat, now, base, raw_rms=None, scare_s=2.0, sleepy_after=45.0,
               hands_off_s=8.0, scare_jump=0.25):
        """raw_rms: loudness before AutoGain. After a silence AutoGain turns the
        scale up, so only the uncalibrated level tells a bang from a whisper."""
        if base != self.base:                       # the user chose something
            if self.base is not None:
                self.hands_off_until = now + hands_off_s
                self.current = None
            self.base = base
        # how long it has been quiet (short gaps between words do not count as loud)
        if feat.rms < 0.08:
            if self.quiet_since is None:
                self.quiet_since = now
        quiet_for = (now - self.quiet_since) if self.quiet_since is not None else 0.0
        loud = feat.rms if raw_rms is None else raw_rms
        jump = loud - self.prev_rms
        self.prev_rms = loud
        if feat.rms >= 0.08:
            self.quiet_since = None
        if feat.beat > 0.0:
            self.beats.append(now)
        self.beats = [t for t in self.beats if now - t < 5.0]

        if now < self.hands_off_until:
            return None
        if quiet_for > 2.0 and jump > scare_jump:
            self.current, self.until = "scared", now + scare_s
        elif self.current == "scared" and now < self.until:
            pass
        elif len(self.beats) >= 6 and feat.rms_slow > 0.10:
            self.current, self.until = "happy", now + 3.0
        elif feat.silence_time > sleepy_after:
            self.current, self.until = "sleepy", now + 0.5
        elif now >= self.until:
            self.current = None
        return self.current


# gestures
class Gestures:
    """Recognises gestures from the motion and distance sensors.

      nod (yes)             -> happy          head pitches up-down-up
      head shake (no)       -> bad            head turns left-right-left
      hand held at the face -> love           "petting": something stays very near
      quick poke at the nose-> sneeze         near and away again at once
      two knocks in a row   -> angry          the second bump shortly after the first

    update() returns (emotion or None, event or None); events are one-shot:
    "nod", "shake", "pet", "boop", "knock".
    """

    RATE_DPS = 60.0         # a swing must be at least this fast
    SWINGS = 3              # this many swings there and back...
    WINDOW_S = 1.4          # ...within this time
    PET_AFTER_S = 0.9
    BOOP_MAX_S = 0.6
    KNOCK_GAP_S = 0.9

    def __init__(self):
        self.swings = {"nod": [], "shake": []}
        self.sign = {"nod": 0, "shake": 0}
        self.near_since = None
        self.petting = False
        self.last_impact = -10.0
        self.emotion = None
        self.until = 0.0
        self.cooldown = 0.0

    def _swing(self, name, rate, now):
        sign = 1 if rate > self.RATE_DPS else -1 if rate < -self.RATE_DPS else 0
        if sign and sign != self.sign[name]:
            self.sign[name] = sign
            self.swings[name].append(now)
        self.swings[name] = [t for t in self.swings[name] if now - t < self.WINDOW_S]
        if len(self.swings[name]) >= self.SWINGS:
            self.swings[name] = []
            self.sign[name] = 0
            return True
        return False

    def _show(self, emotion, seconds, now):
        self.emotion, self.until = emotion, now + seconds

    def update(self, now, gyro=None, near=False, impact=False):
        """gyro: (pitch, roll, yaw) rates in deg/s or None; near: something is
        within a hand's width of the face; impact: a bump was felt this frame."""
        event = None
        if gyro is not None and now >= self.cooldown:
            if self._swing("nod", gyro[0], now):
                event = "nod"
                self._show("happy", 2.5, now)
            elif self._swing("shake", gyro[2], now):
                event = "shake"
                self._show("bad", 2.5, now)
            if event:
                self.cooldown = now + 1.5
                self.swings = {"nod": [], "shake": []}

        if near:
            if self.near_since is None:
                self.near_since = now
            if now - self.near_since > self.PET_AFTER_S:
                if not self.petting:
                    self.petting = True
                    event = "pet"
                self._show("love", 2.0, now)             # as long as the hand stays, and a bit after
        else:
            if (self.near_since is not None and not self.petting
                    and now - self.near_since < self.BOOP_MAX_S):
                event = "boop"
            self.near_since = None
            self.petting = False

        if impact:
            if now - self.last_impact < self.KNOCK_GAP_S:
                event = "knock"
                self._show("angry", 3.0, now)
            self.last_impact = now

        if self.emotion is not None and now >= self.until:
            self.emotion = None
        return self.emotion, event


# winks
class Asymmetry:
    """Lets the two eyes differ for a moment: a wink, or one eye narrowed and
    the other raised (a doubtful look). Happens by itself now and then, in the
    emotions it suits, and on request.

    update() returns ((closure, lift) for eye 0, (closure, lift) for eye 1):
    closure 0..1 shuts the eye, lift raises it by that many pixels.
    """

    WINKERS = ("happy", "love", "bad", "evil")
    DOUBTERS = ("bad", "evil", "angry", "sad")
    STILL = ((0.0, 0.0), (0.0, 0.0))

    def __init__(self):
        self.kind = None
        self.side = 0
        self.t0 = 0.0
        self.length = 0.0
        self.next_t = None

    def trigger(self, now, kind="wink", side=None):
        self.kind = kind
        self.side = random.randint(0, 1) if side is None else int(side) & 1
        self.t0 = now
        self.length = 0.5 if kind == "wink" else random.uniform(1.6, 3.0)

    def update(self, now, emotion, enabled=True, min_gap=9.0, max_gap=22.0):
        if self.next_t is None:
            self.next_t = now + random.uniform(min_gap, max_gap)
        if self.kind is None and enabled and now >= self.next_t:
            kinds = [k for k, who in (("wink", self.WINKERS), ("doubt", self.DOUBTERS)) if emotion in who]
            if kinds:
                self.trigger(now, random.choice(kinds))
            self.next_t = now + random.uniform(min_gap, max_gap)
        if self.kind is None:
            return self.STILL
        t = now - self.t0
        if t >= self.length:
            self.kind = None
            return self.STILL
        if self.kind == "wink":                      # shut quickly, hold, open
            if t < 0.10:
                c = t / 0.10
            elif t < 0.30:
                c = 1.0
            else:
                c = max(0.0, 1.0 - (t - 0.30) / 0.20)
            c = c * c * (3.0 - 2.0 * c)
            one, other = (c, 0.0), (0.0, 0.3 * c)
        else:                                        # doubt: ease in, hold, ease out
            edge = min(1.0, t / 0.3, (self.length - t) / 0.3)
            k = edge * edge * (3.0 - 2.0 * edge)
            one, other = (0.45 * k, -0.5 * k), (0.0, 1.3 * k)
        return (one, other) if self.side == 0 else (other, one)


# sharp sounds
class ClapDetector:
    """A clap, a bang, a knock on the table: a short sound in every band at once.

    Calibrates itself all the time: it keeps the average level of every band
    and the spread of the total over the last couple of seconds, so the same
    clap is caught in a quiet room and in a noisy hall, while steady music or
    a crowd does not set it off. A clap is broadband: most bands jump at once.
    A voice starting after a pause lifts only a few middle bands and does not
    count. The raw bands are used, before the automatic volume, which would
    flatten exactly these short peaks.
    """

    AVG_S = 1.5            # the averages follow the room this fast
    REFRACTORY_S = 0.8     # one clap, one reaction

    def __init__(self):
        self.avg = None
        self.band_avg = None
        self.var = 0.0
        self.last = -10.0

    def reset(self):
        self.avg = None
        self.band_avg = None
        self.var = 0.0

    def update(self, now, bars, dt, sensitivity=0.5):
        """bars: raw band levels 0..1 this frame; sensitivity 0..1. -> True on a clap."""
        b = np.asarray(bars, dtype=np.float32).ravel()
        if b.size == 0:
            return False
        level = float(b.mean())
        sensitivity = min(1.0, max(0.0, float(sensitivity)))
        if self.avg is None or self.band_avg is None or self.band_avg.shape != b.shape:
            self.avg, self.band_avg = level, b.copy()
            return False
        ratio = 3.0 - 1.6 * sensitivity                  # 3x the average .. 1.4x
        sigmas = 6.0 - 3.0 * sensitivity
        jump = level - self.avg
        broad = float(np.mean(b > self.band_avg * ratio + 0.01))    # share of bands that jumped
        hit = (level > self.avg * ratio
               and jump > max(0.05 * (1.2 - sensitivity), sigmas * math.sqrt(self.var))
               and broad >= 0.75 - 0.25 * sensitivity
               and now - self.last > self.REFRACTORY_S)
        if hit:
            self.last = now
        elif now - self.last > 0.25:                     # the clap itself does not raise the average
            a = min(1.0, max(dt, 1e-3) / self.AVG_S)
            self.avg += jump * a
            self.var += (jump * jump - self.var) * a
            self.band_avg += (b - self.band_avg) * a
        return hit


# distance sensor behind the visor
class ToFCalibrator:
    """Finds the visor's own reflection and leaves it out.

    A distance sensor behind a visor often sees the visor itself: a reading
    that stays the same, 30-70 mm, whoever is in front. When the readings stay
    put within a few millimetres for STEADY_S, that is this reflection, and
    everything up to just past it is ignored. calibrate() measures it on
    demand (nothing in front of the visor for a few seconds).
    """

    STEADY_S = 20.0
    MANUAL_S = 5.0
    SPREAD_MM = 6
    MAX_PHANTOM_MM = 80
    MARGIN_MM = 10

    def __init__(self):
        self.samples = deque()
        self.phantom = None
        self.manual_until = None
        self.manual = []

    def calibrate(self, now):
        self.manual_until = now + self.MANUAL_S
        self.manual = []

    @property
    def calibrating(self):
        return self.manual_until is not None

    def update(self, now, mm, valid):
        if self.manual_until is not None:
            if valid and mm is not None:
                self.manual.append(mm)
            if now >= self.manual_until:
                close = [m for m in self.manual if m <= self.MAX_PHANTOM_MM]
                # most readings at one short distance = the visor; nothing near = no reflection
                self.phantom = int(np.median(close)) if len(close) >= max(5, len(self.manual) // 2) else None
                self.manual_until = None
                self.samples.clear()
            return
        if not valid or mm is None:
            return
        self.samples.append((now, mm))
        while self.samples and now - self.samples[0][0] > self.STEADY_S:
            self.samples.popleft()
        if len(self.samples) < 30 or now - self.samples[0][0] < self.STEADY_S * 0.9:
            return
        vals = [m for _, m in self.samples]
        if max(vals) - min(vals) <= self.SPREAD_MM and float(np.median(vals)) <= self.MAX_PHANTOM_MM:
            self.phantom = int(np.median(vals))
        elif self.phantom is not None and min(vals) > self.phantom + 30:
            self.phantom = None              # the reflection is gone (or it was a person standing still)

    def min_valid(self, fallback):
        """Readings nearer than this are not a person."""
        return self.phantom + self.MARGIN_MM if self.phantom is not None else fallback


class BoopSensor:
    """Something right at the nose (a finger), up to boop_mm from the sensor.

    Very close the sensor stops measuring (the ESP drops readings under 30 mm),
    so "a near reading, then no reading at all" also counts as touching, for
    as long as the sensor stays blind (up to BLIND_MAX_S).
    """

    BLIND_AFTER_S = 0.6     # the last near reading was this recent
    BLIND_MAX_S = 6.0
    APPROACH_MM = 30        # "near" for the blind rule: within boop_mm + this

    def __init__(self):
        self.last_near = -10.0
        self.blind_since = None

    def update(self, now, mm, valid, boop_mm=30, phantom=None):
        """-> True while something touches the nose."""
        reading_near = valid and mm is not None and (phantom is None or abs(mm - phantom) > 4)
        if valid and mm is not None:
            self.blind_since = None
            if reading_near and mm <= boop_mm + self.APPROACH_MM:
                self.last_near = now
            return reading_near and mm <= boop_mm
        if self.blind_since is None:
            if now - self.last_near > self.BLIND_AFTER_S:
                return False
            self.blind_since = now
        return now - self.blind_since < self.BLIND_MAX_S
