"""Things the face does by itself: auto gain, emotion from sound, gestures, winks.

Fed once per frame by the renderer. No hardware or shared state here, so
they're easy to test (tests/test_reactions.py).
"""
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
    BOOP_MAX_S = 0.45
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
