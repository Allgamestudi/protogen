"""Per-emotion idle motion: anger shakes, joy bounces, love beats, etc.

Breathing/blinks/saccades are in the renderer; this adds the character on top.
Keep the numbers small, one pixel is a lot on 64x32.
"""
import math

TAU = 2.0 * math.pi


class Motion:
    """Offsets for one frame. Distances are in panel pixels."""
    __slots__ = ("eye_dx", "eye_dy", "eye_grow", "eye_squint", "mouth_grow", "nose_dy")

    def __init__(self, eye_dx=0.0, eye_dy=0.0, eye_grow=0.0, eye_squint=0.0,
                 mouth_grow=0.0, nose_dy=0.0):
        self.eye_dx = eye_dx            # shift of the eye
        self.eye_dy = eye_dy
        self.eye_grow = eye_grow        # the eye gets this much fatter (thinner if negative)
        self.eye_squint = eye_squint    # 0 = open, 1 = shut; adds to the blink
        self.mouth_grow = mouth_grow
        self.nose_dy = nose_dy

    def mixed(self, other, t):
        return Motion(*(getattr(self, k) * (1.0 - t) + getattr(other, k) * t for k in self.__slots__))

    def scaled(self, k):
        return Motion(*(getattr(self, name) * k for name in self.__slots__))


def _wave(t, hz, phase=0.0):
    return math.sin(TAU * hz * t + phase)


def _bursts(t, hz, sharp=3.0):
    """0..1: comes up now and then and goes away again."""
    return max(0.0, _wave(t, hz)) ** sharp


def _noise(t, hz):
    """Smooth wandering value in -1..1 (three sines that never line up)."""
    return (_wave(t, hz) + _wave(t, hz * 1.73, 1.3) + _wave(t, hz * 2.61, 4.1)) / 3.0


def _pulse(phase, width):
    return math.exp(-(phase / width) ** 2)


def _angry(t):
    fit = _bursts(t, 0.21)                               # a fit of rage every few seconds
    return Motion(eye_dx=0.45 * fit * _wave(t, 11.0),
                  eye_squint=0.10 + 0.10 * _wave(t, 0.33) + 0.12 * fit,
                  eye_grow=0.25 * fit,
                  mouth_grow=0.30 * fit * (0.5 + 0.5 * _wave(t, 9.0)))


def _bad(t):
    sly = 0.5 + 0.5 * _wave(t, 0.16)                     # slow sly squint with a side glance
    return Motion(eye_dx=0.8 * _wave(t, 0.11), eye_squint=0.22 * sly,
                  mouth_grow=0.25 * sly)


def _happy(t):
    hop = abs(_wave(t, 0.85))                            # bounces
    return Motion(eye_dy=-0.9 * hop, eye_grow=0.30 * hop, mouth_grow=0.25 * hop,
                  nose_dy=-0.4 * hop)


def _sad(t):
    sob = _bursts(t, 0.17, 4.0)                          # the lip quivers now and then
    return Motion(eye_dy=0.5 + 0.4 * _wave(t, 0.12), eye_squint=0.14 + 0.08 * _wave(t, 0.2),
                  mouth_grow=0.30 * sob * _wave(t, 7.0), nose_dy=0.4 * sob * abs(_wave(t, 3.5)))


def _scared(t):
    return Motion(eye_dx=0.55 * _noise(t, 3.2), eye_dy=0.40 * _noise(t + 9.0, 3.7),
                  eye_grow=0.35 * _wave(t, 2.6), mouth_grow=0.25 * _wave(t, 6.0))


def _surprised(t):
    return Motion(eye_grow=0.55 * _wave(t, 0.7) + 0.2 * _wave(t, 2.3),
                  eye_dy=-0.3 * _wave(t, 0.7), mouth_grow=0.30 * _wave(t, 0.7, 1.0))


def _sleepy(t):
    nod = ((t * 0.13) % 1.0) ** 2                        # sinks slowly, jerks awake
    return Motion(eye_dy=1.2 * nod, eye_squint=0.55 * nod, nose_dy=0.6 * nod)


def _love(t):
    phase = (t * 1.15) % 1.0                             # heartbeat: lub-dub
    beat = _pulse(phase - 0.08, 0.05) + 0.6 * _pulse(phase - 0.30, 0.06)
    return Motion(eye_grow=1.0 * beat, eye_dy=-0.3 * beat, mouth_grow=0.2 * beat)


def _evil(t):
    return Motion(eye_squint=0.16 + 0.14 * _wave(t, 0.1), eye_dx=0.6 * _wave(t, 0.07),
                  eye_grow=0.35 * _bursts(t, 0.29, 6.0), mouth_grow=0.3 * (0.5 + 0.5 * _wave(t, 0.1)))


CHARACTERS = {
    "angry": _angry, "bad": _bad, "happy": _happy, "sad": _sad, "scared": _scared,
    "surprised": _surprised, "sleepy": _sleepy, "love": _love, "evil": _evil,
}


def motion(emotion_from, emotion_to, blend, t, mouth=0.0, beat=0.0, amount=1.0):
    """Motion for this frame.

    blend   0..1 between the two emotions (their characters are mixed)
    mouth   how open the mouth is, 0..1: the eyes lift and widen with speech
    beat    0..1, decaying after a beat: the eyes pop
    amount  0 switches everything off, 1 is normal, 2 is exaggerated
    """
    if amount <= 0.0:
        return Motion()
    still = lambda _t: Motion()
    blend = min(max(blend, 0.0), 1.0)
    m = CHARACTERS.get(emotion_to, still)(t)
    if blend < 1.0 and emotion_from != emotion_to:
        m = CHARACTERS.get(emotion_from, still)(t).mixed(m, blend)
    m.eye_dy -= 0.6 * mouth
    m.eye_grow += 0.30 * mouth + 0.8 * beat
    m.nose_dy -= 0.3 * mouth
    m = m.scaled(amount)
    m.eye_squint = min(max(m.eye_squint, 0.0), 0.8)
    return m
