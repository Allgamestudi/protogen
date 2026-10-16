"""Chrome-style T-Rex runner for the LED panel.

Renders to the full chain width (128 if dual-panel mirror chain, else 64).
Single-button input: each `jump()` call either jumps the dino or, after a
crash, restarts the run.

The renderer drives it once per frame:
    game = dino_game.get(full_w, config.H)
    if state.pop("dino_jump", False): game.jump()
    game.step(dt)
    frame = game.render()
"""
from __future__ import annotations

import random
import numpy as np

import config

# sprite helpers

def _bits(rows):
    return np.array([[1 if c == "#" else 0 for c in r] for r in rows], dtype=np.uint8)

# dino sprites
# 8 px wide, 10 tall. Two run frames (alternating legs), one jump frame,
# one game-over (X eye) frame.

DINO_RUN_1 = _bits([
    "....####",
    "....#.##",
    "....####",
    "....####",
    "#...###.",
    "######..",
    ".#####..",
    "..####..",
    "..##.#..",
    "..#.....",
])
DINO_RUN_2 = _bits([
    "....####",
    "....#.##",
    "....####",
    "....####",
    "#...###.",
    "######..",
    ".#####..",
    "..####..",
    "..#..#..",
    ".....#..",
])
DINO_JUMP = _bits([
    "....####",
    "....#.##",
    "....####",
    "....####",
    "#...###.",
    "######..",
    ".#####..",
    "..####..",
    "..#..#..",
    "..#..#..",
])
DINO_DEAD = _bits([
    "....####",
    "....#x##",
    "....####",
    "....####",
    "#...###.",
    "######..",
    ".#####..",
    "..####..",
    "..#..#..",
    "..#..#..",
])  # 'x' is treated same as '#' here; the visual cue is the red color.
DINO_DEAD = (DINO_DEAD > 0).astype(np.uint8)

# obstacles

CACTUS_SMALL = _bits([
    ".#..",
    "##.#",
    "####",
    ".#..",
    ".#..",
    ".#..",
])
CACTUS_BIG = _bits([
    ".#.#.",
    "##.##",
    "#####",
    ".#.#.",
    ".#.#.",
    ".#...",
    ".#...",
])
CACTUS_DUO = _bits([
    ".#..#..",
    "##.##.#",
    "######.",
    ".#..#..",
    ".#..#..",
    ".#..#..",
])

CACTI = [CACTUS_SMALL, CACTUS_BIG, CACTUS_DUO]

# 3x5 digits
DIGITS = {d: _bits(rows) for d, rows in {
    "0": ["###", "#.#", "#.#", "#.#", "###"],
    "1": [".#.", "##.", ".#.", ".#.", "###"],
    "2": ["###", "..#", "###", "#..", "###"],
    "3": ["###", "..#", ".##", "..#", "###"],
    "4": ["#.#", "#.#", "###", "..#", "..#"],
    "5": ["###", "#..", "###", "..#", "###"],
    "6": ["###", "#..", "###", "#.#", "###"],
    "7": ["###", "..#", ".#.", "#..", "#.."],
    "8": ["###", "#.#", "###", "#.#", "###"],
    "9": ["###", "#.#", "###", "..#", "###"],
}.items()}


# game class

class DinoGame:
    def __init__(self, width: int, height: int):
        self.W = width
        self.H = height
        self.ground_y = height - 3   # last 3 rows = ground area
        # Tunable via state["settings"] dino_* keys (renderer pushes each frame)
        self.dino_x       = 24
        self.gravity      = 320.0
        self.jump_v0      = 92.0
        self.base_speed   = 32.0
        self.speed_ramp   = 0.0009
        self.spawn_min_s  = 0.7
        self.spawn_max_s  = 1.6
        self.reset()

    def reset(self):
        self.dino_y = 0.0           # offset above ground (positive = up)
        self.dino_vy = 0.0
        self.on_ground = True
        self.obstacles: list[dict] = []
        self.dist = 0.0
        self.score = 0
        self.high = max(getattr(self, "high", 0), getattr(self, "score", 0))
        self.speed = self.base_speed
        self.time = 0.0
        self.run_phase = 0
        self.spawn_cd = self.spawn_max_s
        self.game_over = False
        self.flash_t = 0.0

    def jump(self):
        if self.game_over:
            self.reset()
            return
        if self.on_ground:
            self.dino_vy = self.jump_v0
            self.on_ground = False

    def step(self, dt: float):
        self.time += dt
        if self.game_over:
            self.flash_t = (self.flash_t + dt) % 0.8
            return

        self.speed = self.base_speed + self.dist * self.speed_ramp

        if not self.on_ground:
            self.dino_vy -= self.gravity * dt
            self.dino_y += self.dino_vy * dt
            if self.dino_y <= 0:
                self.dino_y = 0
                self.dino_vy = 0
                self.on_ground = True

        self.dist += self.speed * dt
        self.run_phase = int(self.time * 11) % 2

        self.spawn_cd -= dt
        if self.spawn_cd <= 0 and (not self.obstacles or self.obstacles[-1]["x"] < self.W - 28):
            self._spawn()

        for ob in self.obstacles:
            ob["x"] -= self.speed * dt
        self.obstacles = [o for o in self.obstacles if o["x"] > -10]

        self.score = int(self.dist / 4)
        self.high  = max(self.high, self.score)

        if self._collides():
            self.game_over = True
            self.flash_t = 0.0

    def _spawn(self):
        self.obstacles.append({
            "x": float(self.W + 4),
            "sprite": random.choice(CACTI),
        })
        # cooldown scales inversely with speed -> consistent visual spacing
        self.spawn_cd = random.uniform(self.spawn_min_s, self.spawn_max_s) \
                        * (40.0 / max(self.speed, 1.0))

    def _collides(self) -> bool:
        sprite = DINO_JUMP if not self.on_ground else (
            DINO_RUN_1 if self.run_phase == 0 else DINO_RUN_2)
        dh, dw = sprite.shape
        dy = int(round(self.dino_y))
        dx0 = self.dino_x
        dy0 = self.ground_y - dh - dy + 1
        dx1 = dx0 + dw
        dy1 = dy0 + dh
        for ob in self.obstacles:
            sh, sw = ob["sprite"].shape
            ox0 = int(round(ob["x"]))
            oy0 = self.ground_y - sh + 1
            ox1 = ox0 + sw
            oy1 = oy0 + sh
            if dx1 <= ox0 or dx0 >= ox1 or dy1 <= oy0 or dy0 >= oy1:
                continue
            ix0, ix1 = max(dx0, ox0), min(dx1, ox1)
            iy0, iy1 = max(dy0, oy0), min(dy1, oy1)
            a = sprite[iy0 - dy0:iy1 - dy0, ix0 - dx0:ix1 - dx0]
            b = ob["sprite"][iy0 - oy0:iy1 - oy0, ix0 - ox0:ix1 - ox0]
            if (a & b).any():
                return True
        return False

    # rendering
    def render(self) -> np.ndarray:
        frame = np.zeros((self.H, self.W, 3), dtype=np.uint8)
        GROUND_COLOR = (140, 140, 150)
        DINO_COLOR   = (80, 230, 200)
        OB_COLOR     = (230, 200, 90)
        SCORE_COLOR  = (120, 180, 255)
        HIGH_COLOR   = (180, 180, 100)

        # ground line + scrolling dashes one row below
        frame[self.ground_y, :] = GROUND_COLOR
        off = int(self.dist) % 6
        for x in range(-off, self.W, 6):
            xx = x + 1
            if 0 <= xx < self.W:
                frame[self.ground_y + 1, xx] = GROUND_COLOR

        # obstacles
        for ob in self.obstacles:
            self._blit(frame, ob["sprite"],
                       int(round(ob["x"])),
                       self.ground_y - ob["sprite"].shape[0] + 1,
                       OB_COLOR)

        # dino
        if self.game_over:
            sprite = DINO_DEAD
            color = (255, 70, 70) if self.flash_t < 0.4 else (200, 50, 50)
        else:
            sprite = DINO_JUMP if not self.on_ground else (
                DINO_RUN_1 if self.run_phase == 0 else DINO_RUN_2)
            color = DINO_COLOR
        dy = int(round(self.dino_y))
        dy0 = self.ground_y - sprite.shape[0] - dy + 1
        self._blit(frame, sprite, self.dino_x, dy0, color)

        # score top-right
        self._draw_number(frame, self.score, self.W - 1, 1, SCORE_COLOR)
        # high score top-left (only if > 0)
        if self.high > 0:
            self._draw_label_hi(frame, 1, 1)
            self._draw_number(frame, self.high, 15, 1, HIGH_COLOR, right_aligned=False)

        # game-over banner: flash the dino + small "GO" indicator
        if self.game_over and self.flash_t < 0.4:
            # tiny "X" cross above the dino head as visual cue
            cx = self.dino_x + 5
            cy = max(0, dy0 - 4)
            for i in range(3):
                if 0 <= cy + i < self.H:
                    if 0 <= cx + i < self.W: frame[cy + i, cx + i] = (255, 80, 80)
                    if 0 <= cx + 2 - i < self.W: frame[cy + i, cx + 2 - i] = (255, 80, 80)

        return frame

    def _blit(self, frame, sprite, x, y, color):
        sh, sw = sprite.shape
        for sy in range(sh):
            yy = y + sy
            if yy < 0 or yy >= self.H:
                continue
            row = sprite[sy]
            for sx in range(sw):
                if row[sx]:
                    xx = x + sx
                    if 0 <= xx < self.W:
                        frame[yy, xx] = color

    def _draw_number(self, frame, n, anchor_x, top_y, color, right_aligned=True):
        s = str(max(0, int(n)))
        if right_aligned:
            x = anchor_x
            for d in reversed(s):
                g = DIGITS.get(d)
                if g is None: continue
                gh, gw = g.shape
                x -= gw
                self._blit(frame, g, x, top_y, color)
                x -= 1
        else:
            x = anchor_x
            for d in s:
                g = DIGITS.get(d)
                if g is None: continue
                self._blit(frame, g, x, top_y, color)
                x += g.shape[1] + 1

    def _draw_label_hi(self, frame, x, y):
        # tiny "HI" 7x5
        hi = _bits([
            "#.#.##.",
            "#.#..#.",
            "###..#.",
            "#.#..#.",
            "#.#.##.",
        ])
        self._blit(frame, hi, x, y, (90, 110, 130))


# singleton

_game: DinoGame | None = None

def get(width: int, height: int) -> DinoGame:
    global _game
    if _game is None or _game.W != width or _game.H != height:
        _game = DinoGame(width, height)
    return _game
