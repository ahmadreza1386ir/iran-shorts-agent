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
    
