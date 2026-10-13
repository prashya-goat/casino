"""Jackpot wheel renderer (Pillow only, no external services).

render_spin() -> (gif_bytes, duration_seconds)
  * every slice is as big as that player's share of the pot
  * every slice has the player's colour and profile picture
  * the wheel decelerates and stops with the winner's slice under the pointer
render_banner() -> PNG bytes for the lobby message.

All functions are CPU heavy: call them through asyncio.to_thread().
"""
import io
import math
import random
from typing import Optional

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

# colour + matching emoji dot (10 players max)
COLORS = [
    ("#F6B90F", "🟡"), ("#3B8BFF", "🔵"), ("#FF4D5E", "🔴"), ("#2EC27E", "🟢"), ("#A55EEA", "🟣"),
    ("#FF8A3D", "🟠"), ("#8B5A2B", "🟤"), ("#F2F2F2", "⚪"), ("#FF6FB5", "🩷"), ("#7FD6FF", "🩵"),
]
GOLD = (246, 185, 15)
GOLD_DARK = (150, 105, 10)
BG = (9, 10, 18)

OUT = 400          # output size (px)
SS = 2             # supersampling for the wheel disc
FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "DejaVuSans-Bold.ttf",
]


def _font(size: int):
    for p in FONT_PATHS:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def _hex(c: str) -> tuple:
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _shade(rgb: tuple, f: float) -> tuple:
    return tuple(max(0, min(255, int(v * f))) for v in rgb)


def _background(size: int, seed: int = 7) -> Image.Image:
    img = Image.new("RGB", (size, size), BG)
    glow = Image.new("RGB", (size, size), (0, 0, 0))
    c = size // 2
    ImageDraw.Draw(glow).ellipse([c - size * 0.46, c - size * 0.46, c + size * 0.46, c + size * 0.46],
                                 fill=(46, 36, 8))
    img = ImageChops.add(img, glow.filter(ImageFilter.GaussianBlur(size * 0.07)))
    rnd = random.Random(seed)
    d = ImageDraw.Draw(img)
    for _ in range(46):                       # little stars
        x, y, r = rnd.randrange(size), rnd.randrange(size), rnd.choice((1, 1, 1, 2))
        v = rnd.randrange(110, 230)
        d.ellipse([x - r, y - r, x + r, y + r], fill=(v, v, min(255, v + 20)))
    return img


def _circle_avatar(raw: Optional[bytes], d: int, color: tuple, letter: str) -> Image.Image:
    """Round avatar with a gold ring (RGBA, d x d)."""
    big = d * 4
    if raw:
        try:
            im = Image.open(io.BytesIO(raw)).convert("RGB")
            w, h = im.size
            m = min(w, h)
            im = im.crop(((w - m) // 2, (h - m) // 2, (w - m) // 2 + m, (h - m) // 2 + m)).resize((big, big), Image.LANCZOS)
        except Exception:
            raw = None
    if not raw:
        im = Image.new("RGB", (big, big), _shade(color, 0.55))
        ImageDraw.Draw(im).text((big / 2, big / 2), (letter or "?")[:1].upper(), font=_font(int(big * 0.55)),
                                fill=(255, 255, 255), anchor="mm")
    out = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ring = max(10, big // 14)
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).ellipse([ring, ring, big - ring - 1, big - ring - 1], fill=255)
    out.paste(im, (0, 0), mask)
    dr = ImageDraw.Draw(out)
    dr.ellipse([0, 0, big - 1, big - 1], outline=GOLD, width=ring)
    dr.ellipse([ring, ring, big - ring - 1, big - ring - 1], outline=(255, 235, 150), width=max(2, ring // 5))
    return out.resize((d, d), Image.LANCZOS)


def _wheel_disc(slices: list, size: int) -> Image.Image:
    """The rotating disc: coloured slices + gold rim with beads (RGBA, size x size)."""
    big = size * SS
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c, R = big / 2, big / 2 - 2
    rim = R * 0.075
    d.ellipse([c - R, c - R, c + R, c + R], fill=GOLD_DARK)
    box = [c - R + rim, c - R + rim, c + R - rim, c + R - rim]
    for s in slices:
        a0, a1 = s["a0"] - 90, s["a1"] - 90
        col = _hex(s["color"])
        if len(slices) == 1:
            d.ellipse(box, fill=col)
        else:
            d.pieslice(box, a0, a1, fill=col)
            d.pieslice([box[0] + 6, box[1] + 6, box[2] - 6, box[3] - 6], a0, a1, fill=col)
    if len(slices) > 1:                       # thin dividers
        for s in slices:
            ang = math.radians(s["a0"] - 90)
            d.line([c, c, c + (R - rim) * math.cos(ang), c + (R - rim) * math.sin(ang)],
                   fill=_shade(_hex(s["color"]), 0.55), width=max(2, SS * 2))
    # rim: gold ring + beads
    d.ellipse([c - R, c - R, c + R, c + R], outline=GOLD, width=int(rim * 0.55))
    d.ellipse(box, outline=(255, 226, 120), width=max(2, SS * 2))
    bead_r, n = rim * 0.20, 44
    for i in range(n):
        ang = 2 * math.pi * i / n
        x, y = c + (R - rim * 0.5) * math.cos(ang), c + (R - rim * 0.5) * math.sin(ang)
        d.ellipse([x - bead_r, y - bead_r, x + bead_r, y + bead_r], fill=(255, 240, 170))
    return img


def _center(text: str, size: int) -> Image.Image:
    """The black 'POT $x' hub."""
    big = size * 4
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([0, 0, big - 1, big - 1], fill=GOLD)
    d.ellipse([big * 0.05, big * 0.05, big * 0.95, big * 0.95], fill=(14, 14, 22))
    d.ellipse([big * 0.05, big * 0.05, big * 0.95, big * 0.95], outline=GOLD_DARK, width=big // 40)
    d.text((big / 2, big * 0.33), "POT", font=_font(int(big * 0.15)), fill=GOLD, anchor="mm")
    fs = int(big * 0.25)
    f = _font(fs)
    while d.textlength(text, font=f) > big * 0.80 and fs > 20:
        fs -= 6
        f = _font(fs)
    d.text((big / 2, big * 0.60), text, font=f, fill=(255, 255, 255), anchor="mm")
    return img.resize((size, size), Image.LANCZOS)


def _pointer(w: int) -> Image.Image:
    big = w * 4
    img = Image.new("RGBA", (big, int(big * 1.0)), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.polygon([(0, 0), (big, 0), (big / 2, big * 0.95)], fill=GOLD_DARK)
    m = big * 0.09
    d.polygon([(m, m * 0.6), (big - m, m * 0.6), (big / 2, big * 0.95 - m * 1.3)], fill=(255, 205, 40))
    return img.resize((w, w), Image.LANCZOS)


def money_txt(cents: int) -> str:
    return f"${cents // 100:,}.{cents % 100:02d}"


def render_banner(title: str = "JACKPOT", sub: str = "WINNER TAKES THE POT") -> bytes:
    W, H = 800, 300
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    rnd = random.Random(3)
    for _ in range(70):
        x, y, r = rnd.randrange(W), rnd.randrange(H), rnd.choice((1, 1, 2))
        v = rnd.randrange(90, 220)
        d.ellipse([x - r, y - r, x + r, y + r], fill=(v, v, v))
    for y in (14, H - 16):                      # gold light strips
        d.line([(0, y), (W, y)], fill=GOLD, width=4)
        for x in range(10, W, 40):
            d.ellipse([x - 4, y - 4, x + 4, y + 4], fill=(255, 240, 170))
    big = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    bd = ImageDraw.Draw(big)
    f = _font(124)
    bd.text((W / 2 + 4, H * 0.45 + 5), title, font=f, fill=(90, 60, 0), anchor="mm")
    bd.text((W / 2, H * 0.45), title, font=f, fill=GOLD, anchor="mm")
    bd.text((W / 2, H * 0.80), sub, font=_font(30), fill=(235, 235, 245), anchor="mm")
    img.paste(big, (0, 0), big)
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


def _confetti(img: Image.Image, frame: int, rnd: random.Random, cx: int, cy: int) -> None:
    d = ImageDraw.Draw(img)
    for _ in range(26):
        ang = rnd.uniform(0, 2 * math.pi)
        dist = rnd.uniform(40, 190) * (0.35 + frame * 0.10)
        x, y = cx + math.cos(ang) * dist, cy + math.sin(ang) * dist + frame * 7
        col = _hex(rnd.choice(COLORS)[0])
        r = rnd.choice((2, 3, 3, 4))
        d.rectangle([x - r, y - r, x + r, y + r], fill=col)


def render_spin(players: list, winner_idx: int, pot_cents: int, seed: int,
                spin_frames: int = 52) -> tuple[bytes, float]:
    """players: [{'name','cents','color'(hex),'avatar'(bytes|None)}], in slice order.
    winner_idx: index of the winner (already chosen by the game - the wheel only SHOWS it)."""
    rnd = random.Random(seed)
    total = sum(p["cents"] for p in players)

    slices, a = [], 0.0
    for p in players:
        sweep = 360.0 * p["cents"] / total
        slices.append({"a0": a, "a1": a + sweep, "mid": a + sweep / 2, "sweep": sweep, "color": p["color"]})
        a += sweep

    # where the wheel must stop: a random point inside the winner's slice sits under the pointer (top)
    w = slices[winner_idx]
    t = w["a0"] + w["sweep"] * rnd.uniform(0.12, 0.88)
    turns = rnd.choice((3, 4))
    theta_final = turns * 360 + (-t) % 360

    disc_px = int(OUT * 0.92)
    disc = _wheel_disc(slices, disc_px)
    bg = _background(OUT)
    hub = _center(money_txt(pot_cents), int(OUT * 0.27))
    pointer = _pointer(int(OUT * 0.11))
    cx = cy = OUT // 2 + 8
    R = disc_px / 2
    r_av = R * 0.60

    av = []
    for p, s in zip(players, slices):
        d_av = int(max(34, min(R * 0.36, 2 * r_av * math.sin(math.radians(min(s["sweep"], 170) / 2)) * 0.92)))
        av.append(_circle_avatar(p.get("avatar"), d_av, _hex(p["color"]), p["name"][:1]))

    def compose(theta: float, glow: float = 0.0, conf: int = -1) -> Image.Image:
        frame = bg.copy()
        # disc is SS x larger: rotate big, shrink -> smooth edges
        rot = disc.rotate(-theta, resample=Image.BICUBIC).resize((disc_px, disc_px), Image.LANCZOS)
        frame.paste(rot, (cx - disc_px // 2, cy - disc_px // 2), rot)
        for i, (s, im) in enumerate(zip(slices, av)):
            phi = math.radians(s["mid"] + theta)
            x, y = cx + r_av * math.sin(phi), cy - r_av * math.cos(phi)
            if glow and i == winner_idx:
                gd = ImageDraw.Draw(frame)
                rr = im.size[0] / 2 + 6 + glow * 10
                gd.ellipse([x - rr, y - rr, x + rr, y + rr], outline=(255, 245, 160), width=4)
            frame.paste(im, (int(x - im.size[0] / 2), int(y - im.size[1] / 2)), im)
        frame.paste(hub, (cx - hub.size[0] // 2, cy - hub.size[1] // 2), hub)
        frame.paste(pointer, (cx - pointer.size[0] // 2, cy - disc_px // 2 - int(pointer.size[1] * 0.55)), pointer)
        if conf >= 0:
            _confetti(frame, conf, random.Random(seed + conf), cx, cy)
        return frame

    frames, durations = [], []
    for i in range(1, spin_frames + 1):
        tau = i / spin_frames
        ease = 1 - (1 - tau) ** 3
        frames.append(compose(theta_final * ease))
        durations.append(60 if tau < 0.7 else int(60 + (tau - 0.7) / 0.3 * 110))
    for k in range(8):                         # winner pulse + confetti
        frames.append(compose(theta_final, glow=(k % 4) / 3, conf=k))
        durations.append(90)
    frames.append(compose(theta_final, glow=1.0, conf=9))
    durations.append(4000)

    pal = [f.convert("P", palette=Image.ADAPTIVE, colors=96, dither=Image.Dither.NONE) for f in frames]
    out = io.BytesIO()
    pal[0].save(out, "GIF", save_all=True, append_images=pal[1:], duration=durations, loop=0,
                disposal=1, optimize=False)
    return out.getvalue(), sum(durations[:-1]) / 1000.0
