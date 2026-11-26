#!/usr/bin/env python3
"""
Rich storybook & Persian miniature procedural painter (Pillow only) — v16.
Emphasizes non-repetitive compositions, richer lighting/texture layers,
and distinct visual framing across scenes without manual per-proverb coding.
"""

from __future__ import annotations

import hashlib
import math
import random
from typing import Any

from PIL import Image, ImageDraw, ImageFilter

from common import CANVAS_H as H, CANVAS_W as W

Color = tuple[int, int, int]
ICE: Color = (176, 226, 250)
ICE_EDGE: Color = (236, 250, 255)

PALETTES: dict[str, dict[str, Any]] = {
    "bazaar_gold": dict(
        sky=[(255, 224, 158), (238, 154, 82), (168, 88, 52)],
        g1=(194, 132, 74),
        g2=(146, 92, 52),
        g3=(86, 50, 32),
        accent=(246, 194, 64),
        light=(255, 246, 214),
        dark=(44, 24, 16),
        water=((56, 124, 134), (24, 74, 96)),
        air="none",
    ),
    "cool_dusk": dict(
        sky=[(18, 24, 62), (68, 62, 126), (164, 102, 138)],
        g1=(52, 46, 86),
        g2=(34, 30, 62),
        g3=(18, 18, 38),
        accent=(142, 206, 255),
        light=(244, 246, 255),
        dark=(12, 14, 28),
        water=((38, 72, 126), (18, 38, 76)),
        air="fireflies",
    ),
    "story_sunset": dict(
        sky=[(58, 38, 92), (214, 88, 76), (255, 196, 102)],
        g1=(108, 74, 52),
        g2=(72, 48, 38),
        g3=(42, 26, 24),
        accent=(248, 196, 68),
        light=(255, 242, 198),
        dark=(28, 16, 20),
        water=((58, 154, 164), (22, 82, 108)),
        air="none",
    ),
    "desert_day": dict(
        sky=[(74, 154, 218), (186, 218, 232), (255, 226, 174)],
        g1=(236, 182, 102),
        g2=(206, 144, 76),
        g3=(162, 104, 58),
        accent=(244, 188, 58),
        light=(255, 248, 224),
        dark=(58, 34, 22),
        water=((84, 164, 196), (44, 104, 146)),
        air="none",
    ),
    "night_ice": dict(
        sky=[(6, 12, 36), (18, 42, 88), (48, 82, 132)],
        g1=(46, 62, 98),
        g2=(28, 40, 72),
        g3=(16, 24, 46),
        accent=(178, 234, 255),
        light=(234, 248, 255),
        dark=(8, 12, 26),
        water=((48, 96, 148), (24, 54, 98)),
        air="frost",
    ),
    "warm_summer": dict(
        sky=[(104, 184, 232), (252, 218, 162), (255, 186, 114)],
        g1=(174, 110, 64),
        g2=(128, 78, 48),
        g3=(82, 48, 32),
        accent=(244, 124, 146),
        light=(255, 248, 236),
        dark=(48, 26, 20),
        water=((84, 164, 196), (44, 104, 146)),
        air="none",
    ),
}


def _mix(a: Color, b: Color, t: float) -> Color:
    t = max(0.0, min(1.0, t))
    return (
        int(a[0] + (b[0] - a[0]) * t),
        int(a[1] + (b[1] - a[1]) * t),
        int(a[2] + (b[2] - a[2]) * t),
    )


def _shade(c: Color, f: float) -> Color:
    return (
        max(0, min(255, int(c[0] * f))),
        max(0, min(255, int(c[1] * f))),
        max(0, min(255, int(c[2] * f))),
    )


def _hash_seed(key: str) -> int:
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16)


class Canvas:
    def __init__(
        self,
        pal: dict[str, Any],
        seed: int,
        cw: int = W,
        ch: int = H,
        t: float = 0.5,
        phase: float = 0.0,
    ):
        self.pal = pal
        self.seed = seed
        self.rng = random.Random(seed)
        self.cw = cw
        self.ch = ch
        self.t = t
        self.phase = phase
        self.img = Image.new("RGB", (cw, ch))
        self.d = ImageDraw.Draw(self.img, "RGBA")

    def refresh(self) -> None:
        self.d = ImageDraw.Draw(self.img, "RGBA")

    def X(self, v: float) -> float:
        return v * self.cw

    def Y(self, v: float) -> float:
        return v * self.ch

    def poly(
        self,
        pts: list[tuple[float, float]],
        fill: tuple[int, ...],
        outline: tuple[int, ...] | None = None,
        width: int = 1,
    ) -> None:
        self.d.polygon(pts, fill=fill, outline=outline)
        if outline and width > 1:
            self.d.line(pts + [pts[0]], fill=outline, width=width)

    def ell(
        self,
        cx: float,
        cy: float,
        rx: float,
        ry: float,
        fill: tuple[int, ...],
        outline: tuple[int, ...] | None = None,
        width: int = 1,
    ) -> None:
        self.d.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=fill, outline=outline, width=width)

    def glow(self, cx: float, cy: float, r: float, color: Color, strength: int = 200) -> None:
        pad = int(r * 2.6)
        x0, y0 = max(0, int(cx - pad)), max(0, int(cy - pad))
        x1, y1 = min(self.cw, int(cx + pad)), min(self.ch, int(cy + pad))
        if x1 <= x0 or y1 <= y0:
            return
        box = (x0, y0, x1, y1)
        layer = Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0))
        ImageDraw.Draw(layer).ellipse(
            (cx - x0 - r, cy - y0 - r, cx - x0 + r, cy - y0 + r),
            fill=(*color, strength),
        )
        layer = layer.filter(ImageFilter.GaussianBlur(max(4, r * 0.9)))
        base = self.img.crop(box).convert("RGBA")
        self.img.paste(Image.alpha_composite(base, layer).convert("RGB"), box[:2])
        self.refresh()


def sky(c: Canvas, **_: Any) -> None:
    stops: list[Color] = c.pal["sky"]
    shift = 0.03 * math.sin(c.phase * 0.9)
    strip = Image.new("RGB", (1, c.ch))
    px = strip.load()
    for y in range(c.ch):
        t = max(0.0, min(1.0, y / max(1, c.ch - 1) + shift)) * (len(stops) - 1)
        i = min(int(t), len(stops) - 2)
        px[0, y] = _mix(stops[i], stops[i + 1], t - i)
    c.img.paste(strip.resize((c.cw, c.ch), Image.NEAREST))
    c.refresh()

    c.glow(c.cw * 0.5, c.ch * 0.45, c.cw * 0.65, c.pal["accent"], 45)
    band_rng = random.Random(c.seed + 71)
    for k in range(5):
        by = c.Y(0.10 + k * 0.06)
        speed = 0.024 + k * 0.007
        bx = ((band_rng.random() + c.phase * speed) % 1.4 - 0.2) * c.cw
        bw = c.X(0.24 + band_rng.random() * 0.18)
        bh = c.Y(0.016 + band_rng.random() * 0.01)
        c.ell(bx, by, bw, bh, (*c.pal["light"], 30))


def clouds(c: Canvas, y: float = 0.22, n: int = 4, **_: Any) -> None:
    crng = random.Random(c.seed + 313)
    for i in range(n):
        base_x = crng.uniform(0.12, 0.88)
        drift = ((base_x + c.phase * (0.014 + 0.004 * i)) % 1.25) - 0.12
        cx = c.X(drift)
        cy = c.Y(y + crng.uniform(-0.04, 0.05))
        rx = c.X(crng.uniform(0.10, 0.16))
        ry = c.Y(crng.uniform(0.022, 0.036))
        c.ell(cx, cy, rx, ry, (*c.pal["light"], 55))
        c.ell(cx - rx * 0.45, cy + ry * 0.2, rx * 0.65, ry * 0.75, (*c.pal["light"], 45))
        c.ell(cx + rx * 0.45, cy + ry * 0.2, rx * 0.65, ry * 0.75, (*c.pal["light"], 45))


def sun(c: Canvas, x: float = 0.5, y: float = 0.3, r: float = 0.1, **_: Any) -> None:
    pulse = 1.0 + 0.09 * math.sin(c.phase * 2.4)
    cx = c.X(x + 0.025 * (c.t - 0.5))
    cy = c.Y(y - 0.025 * c.t)
    rr = c.X(r)

    ray_len = rr * 4.8
    for idx in range(10):
        ang = c.phase * 0.28 + idx * (2 * math.pi / 10)
        spread = 0.095
        p1 = (cx + math.cos(ang - spread) * ray_len, cy + math.sin(ang - spread) * ray_len)
        p2 = (cx + math.cos(ang + spread) * ray_len, cy + math.sin(ang + spread) * ray_len)
        c.poly([(cx, cy), p1, p2], (*c.pal["light"], 26))

    c.glow(cx, cy, rr * 2.2 * pulse, c.pal["light"], 180)
    c.ell(cx, cy, rr * 1.14, rr * 1.14, (*c.pal["accent"], 95))
    c.ell(cx, cy, rr, rr, (*c.pal["light"], 255))


def moon(c: Canvas, x: float = 0.76, y: float = 0.16, r: float = 0.06, **_: Any) -> None:
    pulse = 1.0 + 0.08 * math.sin(c.phase * 2.0)
    cx = c.X(x + 0.015 * (c.t - 0.5))
    cy = c.Y(y - 0.015 * c.t)
    rr = c.X(r)
    c.glow(cx, cy, rr * 2.8 * pulse, c.pal["light"], 145)
    c.ell(cx, cy, rr, rr, (*c.pal["light"], 255))
    c.ell(cx + rr * 0.35, cy - rr * 0.1, rr * 0.9, rr * 0.9, (*c.pal["sky"][0], 78))


def stars(c: Canvas, n: int = 120, **_: Any) -> None:
    for i in range(n):
        x = c.rng.random() * c.cw
        y = c.rng.random() * c.ch * 0.54
        twinkle = 0.4 + 0.6 * ((math.sin(c.phase * 3.8 + i * 1.3) + 1.0) * 0.5)
        r = c.rng.choice([2, 3]) * (c.cw / 1080) * (0.85 + 0.4 * twinkle)
        c.ell(x, y, r, r, (255, 250, 235, int(240 * twinkle)))


def atmosphere(c: Canvas) -> None:
    mode = c.pal.get("air", "none")
    if mode == "none":
        return
    rng = random.Random(c.seed + 909)
    count = 18 if mode == "fireflies" else 24
    for i in range(count):
        bx = rng.random()
        by = rng.random() * (0.75 if mode == "fireflies" else 0.85)
        spd_x = rng.uniform(-0.03, 0.03)
        spd_y = rng.uniform(-0.04, 0.02) if mode == "fireflies" else rng.uniform(0.03, 0.07)
        swirl = math.sin(c.phase * 2.0 + i * 1.1) * 0.02
        px = ((bx + c.phase * spd_x + swirl) % 1.0) * c.cw
        py = ((by + c.phase * spd_y) % 0.85) * c.ch
        pulse = 0.35 + 0.65 * ((math.sin(c.phase * 3.5 + i * 0.9) + 1.0) * 0.5)
        rad = rng.uniform(2.5, 4.5) * (c.cw / 1080)

        if mode == "fireflies":
            col = (255, 236, 130)
            c.ell(px, py, rad * 2.4, rad * 2.4, (*col, int(55 * pulse)))
            c.ell(px, py, rad, rad, (*col, int(205 * pulse)))
        elif mode == "frost":
            col = (232, 248, 255)
            c.ell(px, py, rad, rad, (*col, int(140 * pulse)))


def dunes(c: Canvas, y: float = 0.62, layers: int = 3, **_: Any) -> None:
    cols = [c.pal["g1"], c.pal["g2"], c.pal["g3"]]
    step = max(20, c.cw // 30)
    for i in range(layers):
        base = c.Y(y + i * 0.09)
        amp = c.Y(0.032 + 0.01 * i)
        ph = c.rng.random() * 6 + c.t * (0.16 * (i + 1))
        fr = 1.4 + i * 0.6
        pts = [(x, base + amp * math.sin(x / c.cw * fr * math.pi * 2 + ph)) for x in range(0, c.cw + step, step)]
        c.poly(pts + [(c.cw, c.ch), (0, c.ch)], (*cols[min(i, 2)], 255))
        c.d.line(pts, fill=(*c.pal["light"], 100 if i == 0 else 58), width=3)


def ground(c: Canvas, y: float = 0.7, **_: Any) -> None:
    top = int(c.Y(y))
    h_span = max(1, c.ch - top)
    strip = Image.new("RGB", (1, h_span))
    px = strip.load()
    for i in range(h_span):
        px[0, i] = _mix(c.pal["g1"], c.pal["g3"], i / h_span)
    c.img.paste(strip.resize((c.cw, h_span), Image.NEAREST), (0, top))
    c.refresh()
    c.d.line((0, top, c.cw, top), fill=(*c.pal["light"], 90), width=2)
    for _i in range(110):
        x, yy = c.rng.random() * c.cw, top + c.rng.random() * h_span
        c.d.line((x, yy, x + c.rng.randint(15, 65) * (c.cw / 1080), yy), fill=(*c.pal["dark"], 48), width=2)


def reeds(c: Canvas, side: str = "left", y: float = 0.68, n: int = 7, **_: Any) -> None:
    rrng = random.Random(c.seed + 515)
    base_x = c.X(0.12 if side == "left" else 0.88)
    base_y = c.Y(y)
    reed_col = (118, 148, 76)
    tip_col = (218, 178, 86)
    for i in range(n):
        rx = base_x + (i - n / 2) * c.X(0.022)
        h_len = c.Y(rrng.uniform(0.10, 0.20))
        sway = math.sin(c.phase * 2.5 + i * 0.65) * c.X(0.018)
        tx = rx + sway
        ty = base_y - h_len
        c.d.line((rx, base_y, tx, ty), fill=(*reed_col, 235), width=4)
        c.ell(tx, ty, 5, 11, (*tip_col, 245))


def yakhchal(c: Canvas, x: float = 0.5, y: float = 0.74, w: float = 0.7, h: float = 0.42, **_: Any) -> None:
    cx, base, hw, hh = c.X(x), c.Y(y), c.X(w) / 2, c.Y(h)
    lit, shade = (220, 170, 108), (146, 100, 64)
    c.ell(cx, base + c.Y(0.01), hw * 1.28, c.Y(0.028), (*c.pal["dark"], 85))
    prof = [
        (hw * (1 - (t / 40) ** 1.7) ** 0.78 * 0.92 + hw * 0.08 * (1 - t / 40), base - hh * t / 40)
        for t in range(41)
    ]
    left = [(cx - px, py) for px, py in prof]
    right = [(cx + px, py) for px, py in prof]
    c.poly(left + [(cx, left[-1][1])] + [(cx, base)], (*lit, 255), outline=(*c.pal["light"], 95), width=2)
    c.poly(right + [(cx, right[-1][1])] + [(cx, base)], (*shade, 255))
    for k in range(1, 22):
        yy = base - hh * k / 22
        half = hw * (1 - (k / 22) ** 1.7) ** 0.78 * 0.92 + hw * 0.08 * (1 - k / 22)
        c.d.line((cx - half, yy, cx + half, yy), fill=(*c.pal["dark"], 60), width=2)
    c.poly(
        [
            (cx - hw * 0.1, base),
            (cx - hw * 0.1, base - hh * 0.12),
            (cx, base - hh * 0.16),
            (cx + hw * 0.1, base - hh * 0.12),
            (cx + hw * 0.1, base),
        ],
        (*c.pal["dark"], 240),
    )
    wall_top = base - hh * 0.13
    c.poly([(cx - hw * 1.2, base), (cx - hw * 1.2, wall_top), (cx + hw * 1.2, wall_top), (cx + hw * 1.2, base)], (*_shade(lit, 0.92), 255))
    c.poly([(cx, base), (cx, wall_top), (cx + hw * 1.2, wall_top), (cx + hw * 1.2, base)], (*_shade(shade, 0.95), 255))
    c.poly([(cx - hw * 0.1, base), (cx - hw * 0.1, wall_top + hh * 0.015), (cx + hw * 0.1, wall_top + hh * 0.015), (cx + hw * 0.1, base)], (*c.pal["dark"], 240))


def brick_wall(c: Canvas, y: float = 0.35, **_: Any) -> None:
    top = int(c.Y(y))
    c.d.rectangle((0, top, c.cw, c.ch), fill=(*_shade(c.pal["g1"], 0.95), 255))
    sc = c.cw / 1080
    row_h = max(16, int(36 * sc))
    brick_w = max(40, int(105 * sc))
    pan_x = int(c.t * brick_w * 0.9)
    for r, yy in enumerate(range(top, c.ch, row_h)):
        c.d.line((0, yy, c.cw, yy), fill=(*c.pal["dark"], 85), width=2)
        offset = (brick_w // 2 if (r % 2) else 0) - pan_x
        for xx in range(offset - brick_w, c.cw + brick_w, brick_w):
            tone = _shade(c.pal["g1"] if (r + (xx + pan_x) // brick_w) % 2 == 0 else c.pal["g2"], c.rng.uniform(0.9, 1.07))
            c.d.rectangle((xx + 2, yy + 2, xx + brick_w - 2, yy + row_h - 2), fill=(*tone, 105), outline=(*c.pal["light"], 32))
            c.d.line((xx, yy, xx, yy + row_h), fill=(*c.pal["dark"], 75), width=2)


def cutaway(c: Canvas, **_: Any) -> None:
    gy = c.Y(0.52)
    c.poly([(0, gy), (c.cw, gy), (c.cw, c.ch), (0, c.ch)], (*c.pal["g2"], 255))
    for i in range(12):
        yy = gy + i * (c.ch - gy) / 12
        c.d.line((0, yy, c.cw, yy + c.rng.randint(-12, 12)), fill=(*c.pal["dark"], 32), width=3)

    cx, hw = c.X(0.5), c.X(0.38)
    dome_pts = [(cx - hw * 0.85, gy)]
    for t_step in range(1, 21):
        ang = math.pi * (1 - t_step / 20)
        dome_pts.append((cx + math.cos(ang) * hw * 0.85, gy - math.sin(ang) * c.Y(0.24)))
    dome_pts.append((cx + hw * 0.85, gy))
    c.poly(dome_pts, (*c.pal["g1"], 255), outline=(*c.pal["light"], 90), width=2)

    inner_pts = [(cx - hw * 0.65, gy)]
    for t_step in range(1, 21):
        ang = math.pi * (1 - t_step / 20)
        inner_pts.append((cx + math.cos(ang) * hw * 0.65, gy - math.sin(ang) * c.Y(0.18)))
    inner_pts.append((cx + hw * 0.65, gy))
    c.poly(inner_pts, (*_shade(c.pal["sky"][-1], 0.85), 255))

    chamber = (cx - hw * 0.72, gy + c.Y(0.05), cx + hw * 0.72, gy + c.Y(0.31))
    c.d.rounded_rectangle(chamber, radius=int(c.X(0.04)), fill=(18, 30, 56, 255), outline=(*c.pal["light"], 130), width=3)

    for k in range(4):
        wy = chamber[1] + c.Y(0.05 + k * 0.045)
        wx = cx + math.sin(c.phase * 2.6 + k) * hw * 0.35
        c.ell(wx, wy, hw * 0.16, 6, (190, 235, 255, 70))

    s = c.X(0.068)
    gap = max(4, int(c.X(0.006)))
    shimmer = int(24 * math.sin(c.phase * 3.0))
    for row in range(3):
        cols_n = 5 - row
        row_w = cols_n * (s + gap) - gap
        start_x = cx - row_w / 2
        for col in range(cols_n):
            x0 = start_x + col * (s + gap)
            y0 = chamber[3] - c.X(0.032) - (row + 1) * (s + gap)
            ice_c = (min(255, ICE[0] + shimmer), min(255, ICE[1] + shimmer), ICE[2])
            c.d.rounded_rectangle((x0, y0, x0 + s, y0 + s), radius=8, fill=(*ice_c, 240), outline=(*ICE_EDGE, 255), width=2)


def pools(c: Canvas, y: float = 0.76, **_: Any) -> None:
    shimmer = 0.025 * math.sin(c.phase * 2.6)
    for idx, (px_n, py_n, rx_n, ry_n) in enumerate([
        (0.35, y, 0.21, 0.028),
        (0.66, y + 0.04, 0.2, 0.026),
        (0.49, y + 0.085, 0.24, 0.03),
    ]):
        cx, cy = c.X(px_n), c.Y(py_n)
        rx, ry = c.X(rx_n * (1 + shimmer)), c.Y(ry_n)
        c.ell(cx, cy + 4, rx * 1.06, ry * 1.25, (*c.pal["dark"], 70))
        c.ell(cx, cy, rx, ry, (*c.pal["water"][0], 220), outline=(*c.pal["light"], 185), width=3)
        for _k in range(7 + idx):
            sx = cx + c.rng.uniform(-rx * 0.55, rx * 0.55) + math.sin(c.phase * 2.2 + _k) * 8
            sy = cy + c.rng.uniform(-ry * 0.35, ry * 0.35)
            c.d.line((sx, sy, sx + c.X(0.03), sy), fill=(*c.pal["light"], 150), width=2)


def render_card(
    title: str = "",
    subtitle: str = "",
    palette: str = "story_sunset",
    width: int = W,
    height: int = H,
    seed_key: str | None = None,
    **_: Any,
) -> Image.Image:
    pal_key = palette if palette in PALETTES else "story_sunset"
    pal = PALETTES[pal_key]
    key = seed_key or f"{title}|{subtitle}|{pal_key}|{width}x{height}"
    seed = _hash_seed(key)

    c = Canvas(pal=pal, seed=seed, cw=width, ch=height, t=0.5, phase=0.25)
    sky(c)

    if pal_key in ("cool_dusk", "night_ice"):
        stars(c, n=90)
        moon(c, x=0.75, y=0.18, r=0.055)
    else:
        sun(c, x=0.52, y=0.27, r=0.085)

    clouds(c, y=0.21, n=4)
    dunes(c, y=0.58, layers=3)
    ground(c, y=0.72)

    variant = seed % 3
    if variant == 0:
        yakhchal(c, x=0.5, y=0.73, w=0.62, h=0.34)
    elif variant == 1:
        cutaway(c)
    else:
        pools(c, y=0.77)

    reeds(c, side="left", y=0.72, n=6)
    reeds(c, side="right", y=0.72, n=6)
    atmosphere(c)

    return c.img.convert("RGB")
.88)
        drift = ((base_x + c.phase * (0.014 + 0.004 * i)) % 1.25) - 0.12
        cx = c.X(drift)
        cy = c.Y(y + crng.uniform(-0.04, 0.05))
        rx = c.X(crng.uniform(0.10, 0.16))
        ry = c.Y(crng.uniform(0.022, 0.036))
        c.ell(cx, cy, rx, ry, (*c.pal["light"], 55))
        c.ell(cx - rx * 0.45, cy + ry * 0.2, rx * 0.65, ry * 0.75, (*c.pal["light"], 45))
        c.ell(cx + rx * 0.45, cy + ry * 0.2, rx * 0.65, ry * 0.75, (*c.pal["light"], 45))


def sun(c: Canvas, x=0.5, y=0.3, r=0.1, **_: Any) -> None:
    pulse = 1.0 + 0.09 * math.sin(c.phase * 2.4)
    cx = c.X(x + 0.025 * (c.t - 0.5))
    cy = c.Y(y - 0.025 * c.t)
    rr = c.X(r)

    ray_len = rr * 4.8
    for idx in range(10):
        ang = c.phase * 0.28 + idx * (2 * math.pi / 10)
        spread = 0.095
        p1 = (cx + math.cos(ang - spread) * ray_len, cy + math.sin(ang - spread) * ray_len)
        p2 = (cx + math.cos(ang + spread) * ray_len, cy + math.sin(ang + spread) * ray_len)
        c.poly([(cx, cy), p1, p2], (*c.pal["light"], 26))

    c.glow(cx, cy, rr * 2.2 * pulse, c.pal["light"], 180)
    c.ell(cx, cy, rr * 1.14, rr * 1.14, (*c.pal["accent"], 95))
    c.ell(cx, cy, rr, rr, (*c.pal["light"], 255))


def moon(c: Canvas, x=0.76, y=0.16, r=0.06, **_: Any) -> None:
    pulse = 1.0 + 0.08 * math.sin(c.phase * 2.0)
    cx = c.X(x + 0.015 * (c.t - 0.5))
    cy = c.Y(y - 0.015 * c.t)
    rr = c.X(r)
    c.glow(cx, cy, rr * 2.8 * pulse, c.pal["light"], 145)
    c.ell(cx, cy, rr, rr, (*c.pal["light"], 255))
    c.ell(cx + rr * 0.35, cy - rr * 0.1, rr * 0.9, rr * 0.9, (*c.pal["sky"][0], 78))


def stars(c: Canvas, n=120, **_: Any) -> None:
    for i in range(n):
        x = c.rng.random() * c.cw
        y = c.rng.random() * c.ch * 0.54
        twinkle = 0.4 + 0.6 * ((math.sin(c.phase * 3.8 + i * 1.3) + 1.0) * 0.5)
        r = c.rng.choice([2, 3]) * (c.cw / 1080) * (0.85 + 0.4 * twinkle)
        c.ell(x, y, r, r, (255, 250, 235, int(240 * twinkle)))


def atmosphere(c: Canvas) -> None:
    mode = c.pal.get("air", "none")
    if mode == "none":
        return
    rng = random.Random(c.seed + 909)
    count = 18 if mode == "fireflies" else 24
    for i in range(count):
        bx = rng.random()
        by = rng.random() * (0.75 if mode == "fireflies" else 0.85)
        spd_x = rng.uniform(-0.03, 0.03)
        spd_y = rng.uniform(-0.04, 0.02) if mode == "fireflies" else rng.uniform(0.03, 0.07)
        swirl = math.sin(c.phase * 2.0 + i * 1.1) * 0.02
        px = ((bx + c.phase * spd_x + swirl) % 1.0) * c.cw
        py = ((by + c.phase * spd_y) % 0.85) * c.ch
        pulse = 0.35 + 0.65 * ((math.sin(c.phase * 3.5 + i * 0.9) + 1.0) * 0.5)
        rad = rng.uniform(2.5, 4.5) * (c.cw / 1080)

        if mode == "fireflies":
            col = (255, 236, 130)
            c.ell(px, py, rad * 2.4, rad * 2.4, (*col, int(55 * pulse)))
            c.ell(px, py, rad, rad, (*col, int(205 * pulse)))
        elif mode == "frost":
            col = (232, 248, 255)
            c.ell(px, py, rad, rad, (*col, int(140 * pulse)))


def dunes(c: Canvas, y=0.62, layers=3, **_: Any) -> None:
    cols = [c.pal["g1"], c.pal["g2"], c.pal["g3"]]
    step = max(20, c.cw // 30)
    for i in range(layers):
        base = c.Y(y + i * 0.09)
        amp, ph, fr = c.Y(0.032 + 0.01 * i), c.rng.random() * 6 + c.t * (0.16 * (i + 1)), 1.4 + i * 0.6
        pts = [(x, base + amp * math.sin(x / c.cw * fr * math.pi * 2 + ph)) for x in range(0, c.cw + step, step)]
        c.poly(pts + [(c.cw, c.ch), (0, c.ch)], (*cols[min(i, 2)], 255))
        c.d.line(pts, fill=(*c.pal["light"], 100 if i == 0 else 58), width=3)


def ground(c: Canvas, y=0.7, **_: Any) -> None:
    top = int(c.Y(y))
    h_span = max(1, c.ch - top)
    strip = Image.new("RGB", (1, h_span))
    px = strip.load()
    for i in range(h_span):
        px[0, i] = _mix(c.pal["g1"], c.pal["g3"], i / h_span)
    c.img.paste(strip.resize((c.cw, h_span), Image.NEAREST), (0, top))
    c.refresh()
    c.d.line((0, top, c.cw, top), fill=(*c.pal["light"], 90), width=2)
    for _i in range(110):
        x, yy = c.rng.random() * c.cw, top + c.rng.random() * h_span
        c.d.line((x, yy, x + c.rng.randint(15, 65) * (c.cw / 1080), yy), fill=(*c.pal["dark"], 48), width=2)


def reeds(c: Canvas, side="left", y=0.68, n=7, **_: Any) -> None:
    rrng = random.Random(c.seed + 515)
    base_x = c.X(0.12 if side == "left" else 0.88)
    base_y = c.Y(y)
    reed_col = (118, 148, 76)
    tip_col = (218, 178, 86)
    for i in range(n):
        rx = base_x + (i - n / 2) * c.X(0.022)
        h_len = c.Y(rrng.uniform(0.10, 0.20))
        sway = math.sin(c.phase * 2.5 + i * 0.65) * c.X(0.018)
        tx = rx + sway
        ty = base_y - h_len
        c.d.line((rx, base_y, tx, ty), fill=(*reed_col, 235), width=4)
        c.ell(tx, ty, 5, 11, (*tip_col, 245))


def yakhchal(c: Canvas, x=0.5, y=0.74, w=0.7, h=0.42, **_: Any) -> None:
    cx, base, hw, hh = c.X(x), c.Y(y), c.X(w) / 2, c.Y(h)
    lit, shade = (220, 170, 108), (146, 100, 64)
    c.ell(cx, base + c.Y(0.01), hw * 1.28, c.Y(0.028), (*c.pal["dark"], 85))
    prof = [
        (hw * (1 - (t / 40) ** 1.7) ** 0.78 * 0.92 + hw * 0.08 * (1 - t / 40), base - hh * t / 40)
        for t in range(41)
    ]
    left = [(cx - px, py) for px, py in prof]
    right = [(cx + px, py) for px, py in prof]
    c.poly(left + [(cx, left[-1][1])] + [(cx, base)], (*lit, 255), outline=(*c.pal["light"], 95), width=2)
    c.poly(right + [(cx, right[-1][1])] + [(cx, base)], (*shade, 255))
    for k in range(1, 22):
        yy = base - hh * k / 22
        half = hw * (1 - (k / 22) ** 1.7) ** 0.78 * 0.92 + hw * 0.08 * (1 - k / 22)
        c.d.line((cx - half, yy, cx + half, yy), fill=(*c.pal["dark"], 60), width=2)
    c.poly(
        [
            (cx - hw * 0.1, base),
            (cx - hw * 0.1, base - hh * 0.12),
            (cx, base - hh * 0.16),
            (cx + hw * 0.1, base - hh * 0.12),
            (cx + hw * 0.1, base),
        ],
        (*c.pal["dark"], 240),
    )
    wall_top = base - hh * 0.13
    c.poly([(cx - hw * 1.2, base), (cx - hw * 1.2, wall_top), (cx + hw * 1.2, wall_top), (cx + hw * 1.2, base)], (*_shade(lit, 0.92), 255))
    c.poly([(cx, base), (cx, wall_top), (cx + hw * 1.2, wall_top), (cx + hw * 1.2, base)], (*_shade(shade, 0.95), 255))
    c.poly([(cx - hw * 0.1, base), (cx - hw * 0.1, wall_top + hh * 0.015), (cx + hw * 0.1, wall_top + hh * 0.015), (cx + hw * 0.1, base)], (*c.pal["dark"], 240))


def brick_wall(c: Canvas, y=0.35, **_: Any) -> None:
    top = int(c.Y(y))
    c.d.rectangle((0, top, c.cw, c.ch), fill=(*_shade(c.pal["g1"], 0.95), 255))
    sc = c.cw / 1080
    row_h = max(16, int(36 * sc))
    brick_w = max(40, int(105 * sc))
    pan_x = int(c.t * brick_w * 0.9)
    for r, yy in enumerate(range(top, c.ch, row_h)):
        c.d.line((0, yy, c.cw, yy), fill=(*c.pal["dark"], 85), width=2)
        offset = (brick_w // 2 if (r % 2) else 0) - pan_x
        for xx in range(offset - brick_w, c.cw + brick_w, brick_w):
            tone = _shade(c.pal["g1"] if (r + (xx + pan_x) // brick_w) % 2 == 0 else c.pal["g2"], c.rng.uniform(0.9, 1.07))
            c.d.rectangle((xx + 2, yy + 2, xx + brick_w - 2, yy + row_h - 2), fill=(*tone, 105), outline=(*c.pal["light"], 32))
            c.d.line((xx, yy, xx, yy + row_h), fill=(*c.pal["dark"], 75), width=2)


def cutaway(c: Canvas, **_: Any) -> None:
    gy = c.Y(0.52)
    c.poly([(0, gy), (c.cw, gy), (c.cw, c.ch), (0, c.ch)], (*c.pal["g2"], 255))
    for i in range(12):
        yy = gy + i * (c.ch - gy) / 12
        c.d.line((0, yy, c.cw, yy + c.rng.randint(-12, 12)), fill=(*c.pal["dark"], 32), width=3)

    cx, hw = c.X(0.5), c.X(0.38)
    dome_pts = [(cx - hw * 0.85, gy)]
    for t in range(1, 21):
        ang = math.pi * (1 - t / 20)
        dome_pts.append((cx + math.cos(ang) * hw * 0.85, gy - math.sin(ang) * c.Y(0.24)))
    dome_pts.append((cx + hw * 0.85, gy))
    c.poly(dome_pts, (*c.pal["g1"], 255), outline=(*c.pal["light"], 90), width=2)

    inner_pts = [(cx - hw * 0.65, gy)]
    for t in range(1, 21):
        ang = math.pi * (1 - t / 20)
        inner_pts.append((cx + math.cos(ang) * hw * 0.65, gy - math.sin(ang) * c.Y(0.18)))
    inner_pts.append((cx + hw * 0.65, gy))
    c.poly(inner_pts, (*_shade(c.pal["sky"][-1], 0.85), 255))

    chamber = (cx - hw * 0.72, gy + c.Y(0.05), cx + hw * 0.72, gy + c.Y(0.31))
    c.d.rounded_rectangle(chamber, radius=int(c.X(0.04)), fill=(18, 30, 56, 255), outline=(*c.pal["light"], 130), width=3)

    for k in range(4):
        wy = chamber[1] + c.Y(0.05 + k * 0.045)
        wx = cx + math.sin(c.phase * 2.6 + k) * hw * 0.35
        c.ell(wx, wy, hw * 0.16, 6, (190, 235, 255, 70))

    s = c.X(0.068)
    gap = max(4, int(c.X(0.006)))
    shimmer = int(24 * math.sin(c.phase * 3.0))
    for row in range(3):
        cols_n = 5 - row
        row_w = cols_n * (s + gap) - gap
        start_x = cx - row_w / 2
        for col in range(cols_n):
            x0 = start_x + col * (s + gap)
            y0 = chamber[3] - c.X(0.032) - (row + 1) * (s + gap)
            ice_c = (min(255, ICE[0] + shimmer), min(255, ICE[1] + shimmer), ICE[2])
            c.d.rounded_rectangle((x0, y0, x0 + s, y0 + s), radius=8, fill=(*ice_c, 240), outline=(*ICE_EDGE, 255), width=2)


def pools(c: Canvas, y=0.76, **_: Any) -> None:
    shimmer = 0.025 * math.sin(c.phase * 2.6)
    for idx, (px_n, py_n, rx_n, ry_n) in enumerate([
        (0.35, y, 0.21, 0.028),
        (0.66, y + 0.04, 0.2, 0.026),
        (0.49, y + 0.085, 0.24, 0.03),
    ]):
        cx, cy = c.X(px_n), c.Y(py_n)
        rx, ry = c.X(rx_n * (1 + shimmer)), c.Y(ry_n)
        c.ell(cx, cy + 4, rx * 1.06, ry * 1.25, (*c.pal["dark"], 70))
        c.ell(cx, cy, rx, ry, (*c.pal["water"][0], 220), outline=(*c.pal["light"], 185), width=3)
        for _k in range(7 + idx):
            sx = cx + c.rng.uniform(-rx * 0.55, rx * 0.55) + math.sin(c.phase * 2.2 + _k) * 8
            sy = cy + c.rng.uniform(-ry * 0.35, ry * 0.35)
            c.d.line((sx, sy, sx + c.X(0.03), sy), fill=(*c.pal["light"], 150), width=2)


def ice_blocks(c: Canvas, x=0.7, y=0.75, cols=3, rows=2, s=0.065, **_: Any) -> None:
    sz = c.X(s)
    gap = max(4, int(c.X(0.005)))
    total_w = cols * (sz + gap) - gap
    start_x = c.X(x) - total_w / 2
    base_y = c.Y(y)
    c.ell(c.X(x), base_y + 4, total_w * 0.56, sz * 0.22, (*c.pal["dark"], 80))
    for r in range(rows):
        for k in range(cols):
            x0, y0 = start_x + k * (sz + gap), base_y - (r + 1) * (sz + gap)
            c.d.rounded_rectangle((x0, y0, x0 + sz, y0 + sz), radius=8, fill=(*ICE, 232), outline=(*ICE_EDGE, 255), width=2)
            glint = (0.15 + 0.65 * ((math.sin(c.phase * 3.0 + k + r) + 1) * 0.5)) * sz
            c.d.line((x0 + glint * 0.25, y0 + 8, x0 + glint, y0 + 8), fill=(255, 255, 255, 235), width=3)


def figure(
    c: Canvas,
    x=0.5,
    y=0.85,
    h=0.4,
    facing=1,
    arm=None,
    kneel=False,
    robe=None,
    walk: float = 0.0,
    pose: str = "idle",
    beard: bool = False,
    smile: bool = True,
    headgear: str = "turban",
    **_: Any,
) -> None:
    e = _ease(c.t)
    walk_dx = walk * (e - 0.5)
    is_walking = abs(walk) > 0.01
    stride = math.sin(c.phase * 6.5) if is_walking else 0.0
    bob = abs(stride) * 0.012 if is_walking else math.sin(c.phase * 2.2) * 0.004

    cx = c.X(x + walk_dx)
    fy = c.Y(y + bob)
    hh = c.Y(h)
    robe_c = tuple(robe) if robe else _mix(c.pal["accent"], c.pal["dark"], 0.35)
    skin = (226, 172, 130)
    top = fy - hh * (0.58 if kneel else 0.74)
    sw, hem = hh * 0.138, hh * (0.215 if kneel else 0.18)

    c.ell(cx, c.Y(y) + 6, hem * 1.18, hh * 0.026, (*c.pal["dark"], 80))

    if is_walking and not kneel:
        for side_f in (-1, 1):
            fx = cx + side_f * stride * hem * 0.55
            c.ell(fx, c.Y(y) - 2, hh * 0.034, hh * 0.017, (*c.pal["dark"], 255))

    lean_norm = 0.0
    if pose == "point_sack":
        lean_norm = 0.012
    elif pose == "scoop_and_smell":
        lean_norm = 0.024 * (1.0 - _ease(min(1.0, c.t * 2.0)))
    elif pose == "reach_water":
        lean_norm = 0.022 * _ease(min(1.0, c.t * 1.6))
    lean = facing * c.X(lean_norm)

    robe_pts = [
        (cx - sw * 0.52 + lean, top + hh * 0.03),
        (cx + lean, top),
        (cx + sw * 0.52 + lean, top + hh * 0.03),
        (cx + sw * 0.78 + lean * 0.5, top + hh * 0.26),
        (cx + hem, fy),
        (cx - hem, fy),
        (cx - sw * 0.78 + lean * 0.5, top + hh * 0.26),
    ]
    c.poly(robe_pts, (*robe_c, 255), outline=(*c.pal["light"], 90), width=2)
    c.poly(
        [(cx + lean, top), (cx + sw * 0.52 + lean, top + hh * 0.03), (cx + sw * 0.78 + lean * 0.5, top + hh * 0.26), (cx + hem, fy), (cx, fy)],
        (*_shade(robe_c, 0.78), 255),
    )
    sash_y = top + hh * 0.34
    c.d.line((cx - sw * 0.66 + lean * 0.4, sash_y, cx + sw * 0.66 + lean * 0.4, sash_y), fill=(*c.pal["accent"], 215), width=max(3, int(hh * 0.016)))
    c.d.line((cx + facing * sw * 0.14 + lean * 0.6, top + hh * 0.12, cx + facing * hem * 0.25, fy - 8), fill=(*_shade(robe_c, 0.64), 140), width=2)

    hr = hh * 0.066
    head_tilt = 0.0
    if pose == "scoop_and_smell" and c.t > 0.45:
        head_tilt = hr * 0.15 * _ease((c.t - 0.45) / 0.55)
    hx = cx + lean + facing * (hr * 0.08 + head_tilt * 0.3)
    hy = top - hr * 0.96 + head_tilt * 0.4
    c.capsule(cx + lean, top + 3, hx, top - hr * 0.35, hr * 0.28, (*_shade(skin, 0.88), 255))
    _draw_face(c, hx, hy, hr, facing=facing, beard=beard, smile=smile, headgear=headgear)

    sh = (cx + facing * sw * 0.46 + lean, top + hh * 0.08)
    hold_grain = False

    if pose == "point_sack":
        p = _ease(min(1.0, c.t * 1.4))
        tgt = (sh[0] + facing * c.X(0.06 + 0.08 * p), sh[1] + c.Y(0.06 - 0.02 * p))
    elif pose == "scoop_and_smell":
        if c.t < 0.45:
            p = _ease(c.t / 0.45)
            tgt = (sh[0] + facing * c.X(0.06 + 0.08 * p), sh[1] + c.Y(0.06 + 0.14 * p))
        else:
            p = _ease((c.t - 0.45) / 0.55)
            hold_grain = True
            start_pt = (sh[0] + facing * c.X(0.14), sh[1] + c.Y(0.20))
            end_pt = (hx + facing * hr * 1.15, hy + hr * 0.35)
            tgt = (start_pt[0] + (end_pt[0] - start_pt[0]) * p, start_pt[1] + (end_pt[1] - start_pt[1]) * p)
    elif pose == "reach_water":
        p = _ease(min(1.0, c.t * 1.4))
        tgt = (sh[0] + facing * c.X(0.08 + 0.14 * p), sh[1] + c.Y(0.02 + 0.03 * p))
    elif pose == "chop_ice":
        swing = math.sin(c.phase * 5.5)
        tgt = (sh[0] + facing * c.X(0.10), sh[1] + c.Y(-0.04 + 0.07 * max(0.0, swing)))
    elif arm:
        p = _ease(min(1.0, c.t * 1.4))
        tgt = (sh[0] + facing * c.X(arm[0] * (0.75 + 0.25 * p)), sh[1] + c.Y(arm[1] * (0.75 + 0.25 * p)))
    else:
        swing_x = stride * 0.035 if is_walking else 0.0
        tgt = (sh[0] + facing * (sw * 0.15 + c.X(swing_x)), sh[1] + hh * 0.24)

    elbow = ((sh[0] + tgt[0]) * 0.5 - facing * hh * 0.025, (sh[1] + tgt[1]) * 0.5 + hh * 0.055)
    c.capsule(*sh, *elbow, hh * 0.023, (*_shade(robe_c, 0.9), 255))
    c.capsule(*elbow, *tgt, hh * 0.019, (*skin, 255))
    c.ell(*tgt, hh * 0.025, hh * 0.022, (*skin, 255), outline=(*c.pal["light"], 75), width=1)

    if hold_grain:
        for g_i in range(8):
            gx = tgt[0] + (g_i - 3.5) * 3
            gy = tgt[1] - 4 + (g_i % 2) * 3
            c.ell(gx, gy, 3.5, 5.5, (*c.pal["accent"], 255))


def donkey(c: Canvas, x=0.6, y=0.8, w=0.6, travel: float = 0.28, **_: Any) -> None:
    e = _ease(c.t)
    stride = math.sin(c.phase * 7.5)
    bob = abs(stride) * c.Y(0.011)
    cx = c.X(x + travel * (0.5 - e))
    gy = c.Y(y) - bob
    bw = c.X(w)
    body, dark = (134, 116, 102), (90, 76, 68)

    c.ell(cx, c.Y(y) + 6, bw * 0.5, c.Y(0.018), (*c.pal["dark"], 85))
    for idx, lx in enumerate((-0.34, -0.2, 0.2, 0.34)):
        swing = stride * bw * 0.065 * (1 if idx % 2 == 0 else -1)
        c.capsule(cx + bw * lx, gy - bw * 0.3, cx + bw * lx + swing, c.Y(y), bw * 0.03, (*dark, 255))

    c.ell(cx, gy - bw * 0.42, bw * 0.42, bw * 0.19, (*body, 255), outline=(*c.pal["light"], 70), width=2)
    head_nod = math.sin(c.phase * 3.8) * bw * 0.03
    hx = cx - bw * 0.56
    hy = gy - bw * 0.74 + head_nod
    c.capsule(cx - bw * 0.32, gy - bw * 0.5, cx - bw * 0.5, gy - bw * 0.72 + head_nod, bw * 0.07, (*body, 255))
    c.ell(hx, hy, bw * 0.1, bw * 0.07, (*body, 255))
    c.ell(cx - bw * 0.64, gy - bw * 0.72 + head_nod, bw * 0.045, bw * 0.04, (*dark, 255))
    c.ell(hx - bw * 0.02, hy - bw * 0.015, bw * 0.016, bw * 0.016, (250, 248, 240, 255))
    c.ell(hx - bw * 0.025, hy - bw * 0.015, bw * 0.009, bw * 0.009, (25, 20, 18, 255))

    ear_flick = math.sin(c.phase * 5.0) * bw * 0.012
    c.capsule(cx - bw * 0.5, gy - bw * 0.79 + head_nod, cx - bw * 0.46 + ear_flick, gy - bw * 0.93 + head_nod, bw * 0.02, (*dark, 255))
    c.capsule(cx - bw * 0.54, gy - bw * 0.8 + head_nod, cx - bw * 0.54 - ear_flick, gy - bw * 0.95 + head_nod, bw * 0.02, (*dark, 255))
    tail_sw = math.sin(c.phase * 6.0) * bw * 0.045
    c.capsule(cx + bw * 0.4, gy - bw * 0.46, cx + bw * 0.46 + tail_sw, gy - bw * 0.26, bw * 0.012, (*dark, 255))
    for off in (-0.12, 0.14):
        sack(c, x=(cx + bw * off) / c.cw, y=(gy - bw * 0.52) / c.ch, w=bw * 0.3 / c.cw)


def sack(c: Canvas, x=0.5, y=0.8, w=0.3, fill=True, **_: Any) -> None:
    cx, by, sw = c.X(x), c.Y(y), c.X(w)
    burlap = (182, 144, 92)
    c.poly(
        [
            (cx - sw * 0.5, by),
            (cx - sw * 0.42, by - sw * 0.7),
            (cx - sw * 0.2, by - sw * 0.95),
            (cx + sw * 0.2, by - sw * 0.95),
            (cx + sw * 0.42, by - sw * 0.7),
            (cx + sw * 0.5, by),
        ],
        (*burlap, 255),
        outline=(*c.pal["light"], 75),
        width=2,
    )
    c.poly(
        [
            (cx + sw * 0.1, by),
            (cx + sw * 0.1, by - sw * 0.95),
            (cx + sw * 0.2, by - sw * 0.95),
            (cx + sw * 0.42, by - sw * 0.7),
            (cx + sw * 0.5, by),
        ],
        (*_shade(burlap, 0.8), 255),
    )
    for r_idx in range(1, 5):
        ry = by - sw * (0.18 * r_idx)
        c.d.line((cx - sw * 0.38, ry, cx + sw * 0.38, ry), fill=(*_shade(burlap, 0.72), 85), width=2)

    c.ell(cx, by - sw * 0.93, sw * 0.24, sw * 0.07, (*_shade(burlap, 0.68), 255))
    if fill:
        mound = c.pal["accent"]
        c.ell(cx, by - sw * 0.98, sw * 0.22, sw * 0.07, (*mound, 255))
        for _i in range(28):
            gx, gy = cx + c.rng.uniform(-sw * 0.2, sw * 0.2), by - sw * 0.98 + c.rng.uniform(-sw * 0.05, sw * 0.03)
            c.ell(gx, gy, 3.5, 5.5, (*_shade(mound, c.rng.uniform(0.82, 1.18)), 255))
    c.d.line((cx - sw * 0.2, by - sw * 0.8, cx + sw * 0.2, by - sw * 0.8), fill=(*c.pal["dark"], 150), width=4)


def scales(c: Canvas, x=0.5, y=0.68, s=0.35, **_: Any) -> None:
    cx, by, sz = c.X(x), c.Y(y), c.X(s)
    brass = (238, 190, 74)
    dark_b = (150, 108, 34)
    c.d.rectangle((cx - sz * 0.28, by - 10, cx + sz * 0.28, by), fill=(*dark_b, 255))
    c.d.rectangle((cx - 5, by - sz * 0.75, cx + 5, by - 10), fill=(*brass, 255))
    beam_y = by - sz * 0.72
    damping = math.exp(-c.t * 2.2)
    tilt = math.sin(c.t * 4.5 * math.pi) * sz * 0.09 * damping
    c.d.line((cx - sz * 0.44, beam_y + tilt, cx + sz * 0.44, beam_y - tilt), fill=(*brass, 255), width=7)
    c.ell(cx, beam_y, 11, 11, (*c.pal["light"], 255))
    for side, dy in ((-1, tilt), (1, -tilt)):
        px = cx + side * sz * 0.42
        py = beam_y + dy
        pan_y = py + sz * 0.34
        sway_pan = math.cos(c.t * 4.5 * math.pi) * sz * 0.018 * damping
        c.d.line((px, py, px - sz * 0.14 + sway_pan, pan_y), fill=(*c.pal["light"], 210), width=2)
        c.d.line((px, py, px + sz * 0.14 + sway_pan, pan_y), fill=(*c.pal["light"], 210), width=2)
        c.ell(px + sway_pan, pan_y, sz * 0.16, 10, (*brass, 255))
        c.ell(px + sway_pan, pan_y - 4, sz * 0.11, 6, (*c.pal["accent"], 255))


def wheat(c: Canvas, n=14, y=0.95, h=0.25, **_: Any) -> None:
    for i in range(n):
        x = (i + 0.5) / n * c.cw + c.rng.uniform(-25, 25)
        sway = math.sin(c.phase * 2.6 + i * 0.4) * c.X(0.038)
        top = c.Y(y - h) + c.rng.uniform(-45, 45)
        c.d.line((x, c.Y(y), x + sway, top), fill=(*c.pal["g1"], 255), width=5)
        for k in range(8):
            frac = 1 - k / 8
            kx = x + sway * frac
            c.ell(kx + (k % 2 * 2 - 1) * 8, top + k * 14, 6, 12, (*c.pal["accent"], 255), outline=(*c.pal["light"], 80), width=1)


def hand(c: Canvas, x=0.5, y=0.55, s=0.3, grains: bool = False, action: str = "scoop", **_: Any) -> None:
    e = _ease(c.t)
    if action == "scoop":
        dy = (0.05 - 0.07 * e) * c.ch
        spread_f = 0.85 + 0.15 * e
        curl = 0.04 * e
    elif action == "release":
        dy = -0.015 * e * c.ch
        spread_f = 1.0 + 0.30 * e
        curl = -0.06 * e
    else:
        dy = math.sin(c.phase * 2.8) * c.Y(0.012)
        spread_f = 1.0
        curl = 0.0

    cx, cy, sz = c.X(x), c.Y(y) + dy, c.X(s)
    skin = (226, 172, 130)
    sh = (172, 118, 84)

    c.poly(
        [
            (cx - sz * 0.085, cy + sz * 1.02),
            (cx + sz * 0.085, cy + sz * 1.02),
            (cx + sz * 0.15, cy + sz * 0.42),
            (cx - sz * 0.15, cy + sz * 0.42),
        ],
        (*_shade(skin, 0.94), 255),
    )
    c.ell(cx, cy + sz * 0.18, sz * 0.27, sz * 0.22, (*skin, 255), outline=(*c.pal["light"], 85), width=2)
    c.capsule(cx - sz * 0.21, cy + sz * 0.18, cx - sz * 0.41 * spread_f, cy - sz * 0.03, sz * 0.044, (*skin, 255), outline=(*c.pal["light"], 60))

    for i, (dx_f, len_f, ang_deg) in enumerate([
        (-0.18, 0.31 - curl, -14 * spread_f),
        (-0.06, 0.36 - curl, -4 * spread_f),
        (0.06, 0.34 - curl, 4 * spread_f),
        (0.17, 0.28 - curl, 14 * spread_f),
    ]):
        a = math.radians(ang_deg)
        bx = cx + sz * dx_f
        by = cy - sz * 0.02
        tx = bx + math.sin(a) * sz * len_f
        ty = by - math.cos(a) * sz * len_f
        r_f = sz * (0.039 if i in (1, 2) else 0.035)
        c.capsule(bx, by, tx, ty, r_f, (*skin, 255), outline=(*c.pal["light"], 55))
        c.ell(tx, ty, r_f * 0.8, r_f * 0.62, (*_shade(skin, 1.06), 210))

    c.ell(cx, cy + sz * 0.2, sz * 0.17, sz * 0.085, (*sh, 80))
    if grains:
        count = max(6, int(36 * (1.0 - 0.72 * e))) if action == "release" else 36
        for _i in range(count):
            c.ell(
                cx + c.rng.uniform(-sz * 0.19, sz * 0.19),
                cy + c.rng.uniform(0.04, 0.20) * sz,
                3.5,
                5.5,
                (*_shade(c.pal["accent"], c.rng.uniform(0.85, 1.15)), 255),
            )


def grain_fall(c: Canvas, x=0.5, y1=0.6, y2=0.78, n=45, **_: Any) -> None:
    span = max(1.0, c.Y(y2) - c.Y(y1))
    for i in range(n):
        gx = c.X(x) + c.rng.gauss(0, c.X(0.045))
        base_off = c.rng.random()
        prog = (base_off + c.phase * 1.15) % 1.0
        gy = c.Y(y1) + prog * span
        c.ell(gx, gy, 4.5, 7.5, (*_shade(c.pal["accent"], c.rng.uniform(0.85, 1.15)), 255))
        c.d.line((gx, gy - 20, gx, gy - 6), fill=(*c.pal["accent"], 95), width=2)


def arches(c: Canvas, y=0.45, **_: Any) -> None:
    cols, aw = 4, c.cw / 4
    for i in range(cols):
        x0, x1 = i * aw + 18, (i + 1) * aw - 18
        top, bot = c.Y(y) - c.Y(0.14), c.Y(y) + c.Y(0.2)
        pts = [(x0, bot), (x0, top + (x1 - x0) * 0.5)]
        for t in range(0, 21):
            a = math.pi * t / 20
            pts.append(
                (
                    (x0 + x1) / 2 - math.cos(a) * (x1 - x0) / 2,
                    top + (x1 - x0) * 0.5 - math.sin(a) * (x1 - x0) * 0.62,
                )
            )
        pts.append((x1, bot))
        c.poly(pts, (*c.pal["g3"], 255), outline=(*c.pal["accent"], 120), width=2)
        inner = [(px * 0.88 + (x0 + x1) / 2 * 0.12, py * 0.97 + c.Y(y) * 0.03) for px, py in pts]
        c.poly(inner, (*_shade(c.pal["sky"][-1], 0.9), 255))
    c.d.rectangle((0, c.Y(y) + c.Y(0.2), c.cw, c.ch), fill=(*c.pal["g2"], 255))
    for i in range(5):
        lx = (i + 0.5) / 5 * c.cw + math.sin(c.phase * 2.2 + i) * 6
        c.d.line(((i + 0.5) / 5 * c.cw, 0, lx, c.Y(0.1)), fill=(*c.pal["dark"], 210), width=3)
        c.glow(lx, c.Y(0.11), c.X(0.028), c.pal["accent"], 210)
        c.ell(lx, c.Y(0.11), c.X(0.013), c.Y(0.016), (*c.pal["light"], 255))


def phone(c: Canvas, x=0.5, y=0.46, w=0.54, **_: Any) -> None:
    pw = c.X(w)
    ph = pw * 1.92
    x0, y0 = c.X(x) - pw / 2, c.Y(y) - ph / 2
    c.glow(c.X(x), c.Y(y), pw * 0.58, c.pal["accent"], 85)
    c.d.rounded_rectangle((x0, y0, x0 + pw, y0 + ph), radius=int(pw * 0.11), fill=(10, 12, 20, 255), outline=(*c.pal["light"], 95), width=2)
    c.d.rounded_rectangle((x0 + 10, y0 + 10, x0 + pw - 10, y0 + ph - 10), radius=int(pw * 0.09), fill=(*c.pal["light"], 255))
    c.d.rounded_rectangle((x0 + pw * 0.36, y0 + 18, x0 + pw * 0.64, y0 + 30), radius=6, fill=(10, 12, 20, 255))

    pad_x = pw * 0.06
    gap_x = pw * 0.03
    cell = (pw - 2 * pad_x - gap_x) / 2
    scroll_y = _ease(c.t) * cell * 0.24
    tones = [(226, 172, 130), (182, 130, 96), (238, 196, 160), (148, 104, 78)]
    shirts = [(70, 110, 150), (160, 82, 72), (76, 134, 102), (130, 96, 156)]
    for r in range(3):
        for k in range(2):
            idx = r * 2 + k
            cx0 = x0 + pad_x + k * (cell + gap_x)
            cy0 = y0 + pw * 0.14 + r * (cell + pw * 0.06) - scroll_y
            c.d.rounded_rectangle(
                (cx0, cy0, cx0 + cell, cy0 + cell),
                radius=14,
                fill=(*_mix(c.pal["sky"][1], c.pal["light"], 0.45), 255),
            )
            t = tones[idx % 4]
            sh_c = shirts[idx % 4]
            px_c = cx0 + cell / 2
            py_c = cy0 + cell * 0.42
            pr = cell * 0.17
            c.ell(px_c, cy0 + cell * 0.86, cell * 0.32, cell * 0.2, (*sh_c, 255))
            c.ell(px_c, py_c - pr * 0.35, pr * 1.05, pr * 0.95, (42, 30, 24, 255))
            c.ell(px_c, py_c, pr * 0.9, pr * 1.05, (*t, 255))
            for side_e in (-1, 1):
                c.ell(px_c + side_e * pr * 0.34, py_c - pr * 0.08, pr * 0.13, pr * 0.11, (255, 255, 255, 255))
                c.ell(px_c + side_e * pr * 0.34, py_c - pr * 0.08, pr * 0.07, pr * 0.07, (30, 22, 18, 255))
            c.d.arc((px_c - pr * 0.28, py_c + pr * 0.15, px_c + pr * 0.28, py_c + pr * 0.45), 15, 165, fill=(120, 58, 50, 230), width=2)
            c.d.line((cx0 + 10, cy0 + cell + 10, cx0 + cell * 0.65, cy0 + cell + 10), fill=(*c.pal["dark"], 140), width=4)


def handshake_pair(c: Canvas, y=0.62, **_: Any) -> None:
    ground(c, y=y + 0.21)
    walk_span = 0.09
    shake = math.sin(c.phase * 7.5) * 0.012 if c.t > 0.48 else 0.0
    figure(
        c,
        x=0.38,
        y=y + 0.22,
        h=0.41,
        facing=1,
        arm=[0.115, 0.02 + shake],
        robe=(70, 110, 150),
        walk=walk_span,
        beard=False,
        smile=True,
        headgear="cap",
    )
    figure(
        c,
        x=0.62,
        y=y + 0.22,
        h=0.41,
        facing=-1,
        arm=[0.115, 0.02 + shake],
        robe=(140, 95, 75),
        walk=-walk_span,
        beard=True,
        smile=True,
        headgear="turban",
    )
    if c.t > 0.45:
        c.glow(c.X(0.5), c.Y(y + 0.13 + shake), c.X(0.06), c.pal["accent"], 105)


def scorpion(c: Canvas, x=0.5, y=0.6, s=0.4, swim_x: float = 0.0, **_: Any) -> None:
    e = _ease(c.t)
    bob_y = math.sin(c.phase * 5.0) * c.Y(0.012)
    wiggle = math.sin(c.phase * 7.0) * 0.09
    cx, cy, L = c.X(x + swim_x * (e - 0.5)), c.Y(y) + bob_y, c.X(s)
    body, dark = (56, 38, 32), (28, 18, 16)
    for side in (-1, 1):
        for i in range(4):
            bx = cx - L * 0.12 + i * L * 0.1
            leg_off = math.sin(c.phase * 9.0 + i) * L * 0.04
            c.capsule(bx, cy + side * L * 0.02, bx - L * 0.06 + i * L * 0.02 + leg_off, cy + side * L * 0.2, L * 0.012, (*dark, 255))
            c.capsule(bx - L * 0.06 + i * L * 0.02 + leg_off, cy + side * L * 0.2, bx - L * 0.1 + i * L * 0.03 + leg_off, cy + side * L * 0.27, L * 0.01, (*dark, 255))
    for i in range(6):
        c.ell(cx + L * 0.05 + i * L * 0.085, cy, L * (0.075 - i * 0.005), L * (0.062 - i * 0.004), (*_shade(body, 1 + 0.05 * i), 255))
    c.ell(cx - L * 0.1, cy, L * 0.11, L * 0.085, (*_shade(body, 1.12), 255), outline=(*c.pal["light"], 65), width=1)
    p0, p1, p2, p3 = (
        (cx + L * 0.52, cy),
        (cx + L * 0.78, cy - L * 0.1),
        (cx + L * 0.7, cy - L * (0.62 + wiggle)),
        (cx + L * (0.22 - wiggle), cy - L * (0.58 + wiggle)),
    )
    for i in range(14):
        px, py = _bezier(p0, p1, p2, p3, i / 13)
        rr = L * (0.05 - i * 0.002)
        c.ell(px, py, rr, rr, (*_shade(body, 1 + 0.03 * i), 255))
    ex, ey = _bezier(p0, p1, p2, p3, 1.0)
    c.poly([(ex - L * 0.04, ey - L * 0.01), (ex + L * 0.02, ey - L * 0.04), (ex - L * 0.1, ey + L * 0.05)], (164, 42, 32, 255))
    for side in (-1, 1):
        arm = (cx - L * 0.28, cy + side * L * 0.12)
        tip = (cx - L * 0.5, cy + side * L * (0.2 + wiggle * 0.3))
        c.capsule(cx - L * 0.16, cy + side * L * 0.04, *arm, L * 0.025, (*body, 255))
        c.capsule(*arm, *tip, L * 0.025, (*body, 255))
        c.ell(tip[0] - L * 0.04, tip[1] + side * L * 0.02, L * 0.065, L * 0.04, (*_shade(body, 1.15), 255))
        c.capsule(tip[0] - L * 0.02, tip[1] - side * L * 0.01, tip[0] - L * 0.12, tip[1] - side * L * 0.04, L * 0.014, (*dark, 255))
    c.ell(cx - L * 0.13, cy - L * 0.025, L * 0.014, L * 0.014, (245, 224, 164, 255))


def water(c: Canvas, y0=0.55, y1=1.0, **_: Any) -> None:
    top, bot = int(c.Y(y0)), int(c.Y(y1))
    h_span = max(1, bot - top)
    strip = Image.new("RGB", (1, h_span))
    px = strip.load()
    for y in range(h_span):
        px[0, y] = _mix(c.pal["water"][0], c.pal["water"][1], y / h_span)
    c.img.paste(strip.resize((c.cw, h_span), Image.NEAREST), (0, top))
    c.refresh()

    step = max(24, c.cw // 28)
    for row in range(10):
        wy = top + (row + 0.5) * (h_span / 10)
        amp = c.Y(0.005 + 0.002 * (row % 3))
        freq = 2.0 + (row % 3) * 0.8
        speed = (2.6 + row * 0.25) * (1 if row % 2 == 0 else -1)
        pts = [
            (x, wy + amp * math.sin(x / c.cw * freq * math.pi * 2 + c.phase * speed + row))
            for x in range(0, c.cw + step, step)
        ]
        c.d.line(pts, fill=(*c.pal["light"], 72), width=3)


def ripples(c: Canvas, x=0.5, y=0.6, n=4, r=0.08, **_: Any) -> None:
    expand = (c.phase * 0.85) % 1.0
    for i in range(1, n + 1):
        scale = i - 1 + expand
        rx, ry = c.X(r) * scale * 0.9, c.X(r) * scale * 0.32
        alpha = max(25, int(225 * (1 - scale / (n + 0.5))))
        c.d.ellipse((c.X(x) - rx, c.Y(y) - ry, c.X(x) + rx, c.Y(y) + ry), outline=(*c.pal["light"], alpha), width=3)


def leaf(c: Canvas, x=0.5, y=0.68, s=0.22, ang=0, drift: float = 0.06, **_: Any) -> None:
    e = _ease(c.t)
    drift_x = drift * (e - 0.5)
    drift_y = math.cos(c.phase * 2.8) * 0.012
    cx, cy, L = c.X(x + drift_x), c.Y(y + drift_y), c.X(s)
    a = math.radians(ang + math.sin(c.phase * 2.6) * 6.0)
    pts = []
    for t in range(0, 21):
        u = t / 20
        pts.append((u * L - L / 2, -math.sin(u * math.pi) * L * 0.16))
    pts += [(L / 2 - u * L, math.sin(u * math.pi) * L * 0.12) for u in [i / 20 for i in range(21)]]
    rot = [(cx + px * math.cos(a) - py * math.sin(a), cy + px * math.sin(a) + py * math.cos(a)) for px, py in pts]
    c.poly(rot, (98, 156, 72, 255), outline=(168, 212, 118, 120), width=2)
    c.d.line((cx - L / 2 * math.cos(a), cy - L / 2 * math.sin(a), cx + L / 2 * math.cos(a), cy + L / 2 * math.sin(a)), fill=(58, 98, 48, 255), width=4)


def frog_on_stone(c: Canvas, x=0.52, y=0.66, s=0.24, **_: Any) -> None:
    cx, cy, sz = c.X(x), c.Y(y), c.X(s)
    ripples(c, x=x, y=y + 0.03, n=4, r=s * 0.46)
    stone_col = (104, 96, 88)
    c.ell(cx, cy + sz * 0.11, sz * 0.48, sz * 0.18, (*_shade(stone_col, 0.78), 255))
    c.ell(cx, cy + sz * 0.08, sz * 0.44, sz * 0.15, (*stone_col, 255), outline=(*c.pal["light"], 95), width=2)
    puff = 1.0 + 0.26 * max(0.0, math.sin(c.phase * 5.2))
    frog_col = (94, 158, 86)
    c.ell(cx, cy - sz * 0.06, sz * 0.22, sz * 0.14, (*frog_col, 255), outline=(*c.pal["light"], 90), width=2)
    c.ell(cx + sz * 0.12, cy - sz * 0.05, sz * 0.11 * puff, sz * 0.09 * puff, (168, 216, 128, 235))
    for ex_off in (-0.06, 0.08):
        ex, ey = cx + sz * ex_off, cy - sz * 0.20
        c.ell(ex, ey, sz * 0.055, sz * 0.055, (*frog_col, 255))
        c.ell(ex, ey, sz * 0.032, sz * 0.032, (250, 246, 224, 255))
        c.ell(ex + sz * 0.01, ey, sz * 0.016, sz * 0.018, (24, 20, 18, 255))


def lantern(c: Canvas, x=0.5, y=0.45, s=0.24, **_: Any) -> None:
    sway = math.sin(c.phase * 2.4) * c.X(0.015)
    cx, cy, sz = c.X(x) + sway, c.Y(y), c.X(s)
    c.d.line((c.X(x), 0, cx, cy - sz * 0.45), fill=(*c.pal["dark"], 200), width=3)
    c.glow(cx, cy, sz * 0.95, c.pal["accent"], 165)
    brass = (224, 174, 68)
    c.poly([(cx - sz * 0.22, cy - sz * 0.42), (cx + sz * 0.22, cy - sz * 0.42), (cx + sz * 0.14, cy - sz * 0.56), (cx - sz * 0.14, cy - sz * 0.56)], (*brass, 255))
    c.poly([(cx - sz * 0.18, cy - sz * 0.42), (cx + sz * 0.18, cy - sz * 0.42), (cx + sz * 0.24, cy + sz * 0.28), (cx - sz * 0.24, cy + sz * 0.28)], (*c.pal["light"], 215), outline=(*brass, 255), width=3)
    flame_h = sz * (0.14 + 0.03 * math.sin(c.phase * 7.0))
    c.ell(cx, cy + sz * 0.02, sz * 0.08, flame_h, (*c.pal["accent"], 255))
    c.ell(cx, cy + sz * 0.05, sz * 0.045, flame_h * 0.65, (255, 252, 230, 255))
    c.poly([(cx - sz * 0.28, cy + sz * 0.28), (cx + sz * 0.28, cy + sz * 0.28), (cx + sz * 0.18, cy + sz * 0.40), (cx - sz * 0.18, cy + sz * 0.40)], (*brass, 255))


def auto_prop(c: Canvas, topic: str = "", idx: int = 0, **_: Any) -> None:
    sub_rng = random.Random(_hash_seed(f"{c.seed}:{topic}:{idx}"))
    kind = sub_rng.choice(["clay_jar", "soft_hills", "warm_glow"])
    px = sub_rng.uniform(0.22, 0.78)
    py = sub_rng.uniform(0.70, 0.85)
    sc = sub_rng.uniform(0.16, 0.26)
    cx, cy, sz = c.X(px), c.Y(py), c.X(sc)

    if kind == "soft_hills":
        c.poly(
            [(0, c.Y(0.64)), (c.X(0.32), c.Y(0.50)), (c.X(0.66), c.Y(0.58)), (c.cw, c.Y(0.48)), (c.cw, c.Y(0.66)), (0, c.Y(0.66))],
            (*_shade(c.pal["g2"], 0.9), 255),
            outline=(*c.pal["light"], 55),
            width=2,
        )
    elif kind == "clay_jar":
        clay = (176, 108, 72)
        c.ell(cx, cy + 4, sz * 0.25, sz * 0.06, (*c.pal["dark"], 75))
        c.ell(cx, cy - sz * 0.22, sz * 0.22, sz * 0.28, (*clay, 255), outline=(*c.pal["light"], 75), width=2)
        c.d.rectangle((cx - sz * 0.10, cy - sz * 0.52, cx + sz * 0.10, cy - sz * 0.42), fill=(*_shade(clay, 1.1), 255))
        c.ell(cx, cy - sz * 0.52, sz * 0.13, sz * 0.035, (*c.pal["light"], 130))
    else:
        c.glow(cx, c.Y(0.55), sz * 0.85, c.pal["accent"], 55)


def glasses(c: Canvas, y=0.72, **_: Any) -> None:
    ty = c.Y(y)
    c.d.rectangle((0, ty, c.cw, c.ch), fill=(*c.pal["g2"], 255))
    c.d.rectangle((0, ty, c.cw, ty + 16), fill=(*c.pal["g1"], 255))
    for cx_n, sc in ((0.26, 0.8), (0.5, 1.0), (0.74, 0.8)):
        cx, gw = c.X(cx_n), c.X(0.24 * sc)
        top = ty - c.Y(0.2 * sc)
        c.ell(cx, ty + 3, gw * 0.5, 10, (*c.pal["dark"], 95))
        c.d.rectangle((cx - 5, top + c.Y(0.13 * sc), cx + 5, ty - 6), fill=(*c.pal["light"], 195))
        c.ell(cx, ty - 5, gw * 0.3, 10, (*c.pal["light"], 205))
        bowl = [(cx - gw / 2, top), (cx + gw / 2, top), (cx + gw * 0.28, top + c.Y(0.14 * sc)), (cx - gw * 0.28, top + c.Y(0.14 * sc))]
        c.poly(bowl, (255, 255, 255, 95))
        fill = [(cx - gw * 0.46, top + c.Y(0.035 * sc)), (cx + gw * 0.46, top + c.Y(0.035 * sc)), (cx + gw * 0.27, top + c.Y(0.135 * sc)), (cx - gw * 0.27, top + c.Y(0.135 * sc))]
        c.poly(fill, (*_mix(c.pal["accent"], (255, 255, 255), 0.45), 255))
        for k in range(10):
            yy = top + c.Y(0.045 * sc) + k * c.Y(0.009 * sc)
            c.d.arc((cx - gw * 0.4, yy - 10, cx + gw * 0.4, yy + 10), 0, 180, fill=(255, 255, 255, 225), width=3)
        c.ell(cx, top + c.Y(0.035 * sc), gw * 0.46, 12, (*_mix(c.pal["accent"], (255, 255, 255), 0.25), 255))
        c.ell(cx + gw * 0.1, top + c.Y(0.03 * sc), 9, 6, (110, 170, 70, 255))
        c.d.polygon(bowl, outline=(*c.pal["light"], 235), width=3)
        for m in range(3):
            prog = (c.phase * 0.75 + cx_n + m * 0.33) % 1.0
            mist_y = top - c.Y(0.015 + 0.06 * prog)
            alpha = int(110 * (1.0 - prog))
            c.ell(cx + math.sin(c.phase * 3.0 + m + cx_n * 5) * 12, mist_y, gw * 0.24, 10, (240, 250, 255, alpha))


def vignette(c: Canvas, **_: Any) -> None:
    mask = Image.new("L", (c.cw // 8, c.ch // 8), 0)
    ImageDraw.Draw(mask).ellipse((-c.cw // 24, -c.ch // 24, c.cw // 8 + c.cw // 24, c.ch // 8 + c.ch // 24), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(24)).resize((c.cw, c.ch))
    dark = Image.new("RGB", (c.cw, c.ch), (0, 0, 0))
    c.img = Image.composite(c.img, Image.blend(c.img, dark, 0.42), mask)
    c.refresh()


LAYERS: dict[str, Callable[..., None]] = {
    "clouds": clouds,
    "reeds": reeds,
    "sun": sun,
    "moon": moon,
    "stars": stars,
    "dunes": dunes,
    "ground": ground,
    "yakhchal": yakhchal,
    "brick_wall": brick_wall,
    "cutaway": cutaway,
    "pools": pools,
    "ice_blocks": ice_blocks,
    "figure": figure,
    "donkey": donkey,
    "sack": sack,
    "scales": scales,
    "wheat": wheat,
    "hand": hand,
    "grain_fall": grain_fall,
    "arches": arches,
    "phone": phone,
    "handshake_pair": handshake_pair,
    "scorpion": scorpion,
    "water": water,
    "ripples": ripples,
    "leaf": leaf,
    "frog_on_stone": frog_on_stone,
    "lantern": lantern,
    "auto_prop": auto_prop,
    "glasses": glasses,
}


def render_card_frame(
    spec: dict[str, Any],
    seed: int = 1,
    t: float = 0.5,
    phase: float = 0.0,
    cw: int = WIDTH,
    ch: int = HEIGHT,
    motion: str | None = None,
) -> Image.Image:
    pal = PALETTES.get(spec.get("palette", "desert_day"), PALETTES["desert_day"])
    c = Canvas(pal, seed, cw=cw, ch=ch, t=t, phase=phase)
    sky(c)
    for layer in spec.get("layers", []):
        name, kw = (layer, {}) if isinstance(layer, str) else (layer[0], layer[1] if len(layer) > 1 else {})
        fn = LAYERS.get(name)
        if fn is None:
            raise KeyError(f"Unknown card layer: {name}")
        fn(c, **kw)
    atmosphere(c)
    vignette(c)

    e = _ease(t)
    if motion in ("pan_right", "pan_left"):
        z = 1.07
        fx = (0.24 + 0.52 * e) if motion == "pan_right" else (0.76 - 0.52 * e)
        fy = 0.50
    elif motion in ("drift_up", "drift_down"):
        z = 1.07
        fx = 0.50
        fy = (0.72 - 0.44 * e) if motion == "drift_up" else (0.28 + 0.44 * e)
    elif motion == "pull_out":
        z = 1.08 - 0.08 * e
        fx, fy = 0.50, 0.48
    else:
        z = 1.00 + 0.08 * e
        fx, fy = 0.50, 0.46

    if z > 1.001:
        nw, nh = cw / z, ch / z
        left = (cw - nw) * fx
        top = (ch - nh) * fy
        c.img = c.img.crop((left, top, left + nw, top + nh)).resize((cw, ch), Image.BILINEAR)
    return c.img


def render_card(spec: dict[str, Any], seed: int = 1) -> Image.Image:
    return render_card_frame(spec, seed=seed, t=0.45, phase=1.2, cw=W, ch=H, motion=None)
