"""'Visor' — an original animated robot-helmet face for the assistant screen.

Renders one animated GIF per mood (neutral.gif, thinking.gif, happy.gif, ...) that the
xiaozhi firmware shows in the middle of the screen.

    python3 character.py OUT_DIR [--size 300]
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

SS = 3  # supersampling factor for smooth edges

CYAN = (46, 230, 255)
AMBER = (255, 168, 64)     # shown while Claude answers ("cool")
BLUE = (90, 140, 255)
RED = (255, 77, 61)
PINK = (255, 95, 176)
SHELL_TOP = (58, 64, 78)
SHELL_BOTTOM = (20, 23, 30)
VISOR = (4, 6, 10)

FONT_PATHS = [
    "/usr/share/fonts/opentype/inter/Inter-SemiBold.otf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def _font(px: int) -> ImageFont.ImageFont:
    for p in FONT_PATHS:
        if Path(p).exists():
            return ImageFont.truetype(p, px)
    return ImageFont.load_default()


def _lerp(a, b, t):
    return tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))


def _ease(x: float) -> float:
    return 0.5 - 0.5 * math.cos(math.pi * max(0.0, min(1.0, x)))


def _pulse(t: float, start: float, length: float) -> float:
    """0 → 1 → 0 bump between start and start+length (t in [0,1))."""
    if not start <= t <= start + length:
        return 0.0
    return math.sin(math.pi * (t - start) / length)


# ---------------------------------------------------------------------------
# Mood definitions: how the eyes / mouth behave over one loop (t from 0 to 1)
# ---------------------------------------------------------------------------
MOODS = {
    "neutral":     dict(color=CYAN,  eyes="capsule", mouth="dots",  frames=40, blink=True, drift=True),
    "thinking":    dict(color=CYAN,  eyes="look_up", mouth="think", frames=30, scan=True),
    "happy":       dict(color=CYAN,  eyes="arc",     mouth="eq",    frames=24, bounce=True),
    "cool":        dict(color=AMBER, eyes="shades",  mouth="eq",    frames=30, glint=True),
    "sleepy":      dict(color=CYAN,  eyes="closed",  mouth="dots",  frames=40, dim=True, zzz=True),
    "sad":         dict(color=BLUE,  eyes="sad",     mouth="frown", frames=30),
    "crying":      dict(color=BLUE,  eyes="sad",     mouth="frown", frames=30, tears=True),
    "angry":       dict(color=RED,   eyes="angry",   mouth="flat",  frames=20, shake=True),
    "surprised":   dict(color=CYAN,  eyes="round",   mouth="o",     frames=24, pop=True),
    "loving":      dict(color=PINK,  eyes="heart",   mouth="dots",  frames=24, beat=True),
    "winking":     dict(color=CYAN,  eyes="wink",    mouth="eq",    frames=24),
    "laughing":    dict(color=CYAN,  eyes="arc",     mouth="eq_fast", frames=16, shake=True),
    "embarrassed": dict(color=CYAN,  eyes="arc",     mouth="dots",  frames=30, blush=True),
    "confused":    dict(color=CYAN,  eyes="confused", mouth="flat", frames=30, question=True),
    "relaxed":     dict(color=CYAN,  eyes="closed_happy", mouth="dots", frames=40, dim=True),
    # assistant-specific moods
    "alarm":       dict(color=AMBER, eyes="round",   mouth="o",     frames=16, pop=True, shake=True, ringing=True),
    "timer":       dict(color=CYAN,  eyes="clock",   mouth="dots",  frames=24),
}
# Moods that reuse another animation (copied under their own file name).
ALIASES = {
    "shocked": "surprised", "kissy": "loving", "funny": "laughing", "silly": "winking",
    "delicious": "happy", "confident": "cool",
}


class Face:
    def __init__(self, size: int) -> None:
        self.size = size
        self.u = size * SS  # drawing unit = full canvas width at supersampled scale
        self._shell = self._make_shell()

    # ---------------------------------------------------------------- static
    def _gradient(self, w, h, top, bottom) -> Image.Image:
        g = Image.new("RGB", (1, h))
        for y in range(h):
            g.putpixel((0, y), _lerp(top, bottom, y / max(1, h - 1)))
        return g.resize((w, h))

    def _make_shell(self) -> Image.Image:
        """Helmet, ear pods, fin and visor glass: everything that doesn't animate."""
        u = self.u
        img = Image.new("RGB", (u, u), (0, 0, 0))
        d = ImageDraw.Draw(img)
        P = lambda *v: [int(x * u) for x in v]

        # ear pods
        for x0, x1 in ((0.03, 0.12), (0.88, 0.97)):
            d.rounded_rectangle(P(x0, 0.38, x1, 0.62), radius=int(0.035 * u), fill=(30, 34, 43))
            d.rounded_rectangle(P(x0, 0.38, x1, 0.62), radius=int(0.035 * u),
                                outline=(70, 78, 94), width=max(1, int(0.006 * u)))
        # top fin
        d.rounded_rectangle(P(0.44, 0.06, 0.56, 0.16), radius=int(0.03 * u), fill=(44, 49, 60))

        # helmet with vertical gradient
        mask = Image.new("L", (u, u), 0)
        ImageDraw.Draw(mask).rounded_rectangle(P(0.10, 0.12, 0.90, 0.88), radius=int(0.22 * u), fill=255)
        img.paste(self._gradient(u, u, SHELL_TOP, SHELL_BOTTOM), (0, 0), mask)
        d.rounded_rectangle(P(0.10, 0.12, 0.90, 0.88), radius=int(0.22 * u),
                            outline=(92, 102, 120), width=max(1, int(0.007 * u)))
        # soft highlight across the top of the helmet
        hl = Image.new("L", (u, u), 0)
        ImageDraw.Draw(hl).ellipse(P(0.22, 0.13, 0.78, 0.25), fill=60)
        hl = hl.filter(ImageFilter.GaussianBlur(0.03 * u))
        img = Image.composite(Image.new("RGB", (u, u), (255, 255, 255)), img, ImageChops.multiply(hl, mask))
        d = ImageDraw.Draw(img)

        # panel seam lines
        seam = (34, 38, 47)
        w = max(1, int(0.006 * u))
        d.line(P(0.50, 0.12, 0.50, 0.26), fill=seam, width=w)
        d.line(P(0.30, 0.66, 0.20, 0.80), fill=seam, width=w)
        d.line(P(0.70, 0.66, 0.80, 0.80), fill=seam, width=w)

        # visor glass
        d.rounded_rectangle(P(0.17, 0.28, 0.83, 0.62), radius=int(0.13 * u), fill=VISOR,
                            outline=(16, 26, 34), width=max(1, int(0.012 * u)))
        sheen = Image.new("L", (u, u), 0)
        ImageDraw.Draw(sheen).polygon(P(0.24, 0.30, 0.40, 0.30, 0.26, 0.60, 0.19, 0.60), fill=18)
        vmask = Image.new("L", (u, u), 0)
        ImageDraw.Draw(vmask).rounded_rectangle(P(0.18, 0.29, 0.82, 0.61), radius=int(0.12 * u), fill=255)
        img = Image.composite(Image.new("RGB", (u, u), (255, 255, 255)), img,
                              ImageChops.multiply(sheen, vmask))
        self.visor_mask = vmask
        return img

    # ------------------------------------------------------------- animated
    def render(self, mood: str, t: float) -> Image.Image:
        spec = MOODS[mood]
        u = self.u
        color = spec["color"]
        dim = 0.55 if spec.get("dim") else 1.0

        dx = dy = 0.0
        if spec.get("shake"):
            dx = math.sin(t * 2 * math.pi * 4) * 0.008
        if spec.get("bounce"):
            dy = -abs(math.sin(t * 2 * math.pi * 2)) * 0.015

        glow = Image.new("L", (u, u), 0)   # light emitted inside the visor
        extra = Image.new("L", (u, u), 0)  # light outside the visor (mouth, fin, ears)
        g = ImageDraw.Draw(glow)
        e = ImageDraw.Draw(extra)
        self._eyes(g, spec, t)
        self._mouth(e, spec, t)

        # fin light + ear rings (steady, so unchanged pixels compress away)
        breathe = 0.85
        e.ellipse(self._P(0.485, 0.075, 0.515, 0.105), fill=int(255 * breathe))
        for x in (0.075, 0.925):
            e.rounded_rectangle(self._P(x - 0.008, 0.44, x + 0.008, 0.56),
                                radius=int(0.008 * u), fill=int(200 * breathe))

        if spec.get("scan"):  # light sweep across the visor
            x = 0.18 + 0.64 * _ease((t * 1.0) % 1.0)
            band = Image.new("L", (u, u), 0)
            ImageDraw.Draw(band).rectangle(self._P(x - 0.015, 0.29, x + 0.015, 0.61), fill=90)
            band = band.filter(ImageFilter.GaussianBlur(0.02 * u))
            glow = ImageChops.lighter(glow, ImageChops.multiply(band, self.visor_mask))

        glow = ImageChops.multiply(glow, self.visor_mask)
        light = ImageChops.lighter(glow, extra)

        frame = self._shell.copy()
        halo = light.filter(ImageFilter.GaussianBlur(0.035 * u)).point(lambda v: int(v * 0.9 * dim))
        core = light.point(lambda v: int(v * dim))
        frame = Image.composite(Image.new("RGB", (u, u), color), frame, halo)
        frame = Image.composite(Image.new("RGB", (u, u), _lerp(color, (255, 255, 255), 0.45)), frame, core)

        d = ImageDraw.Draw(frame)
        if spec.get("blush"):
            for x in (0.24, 0.70):
                for k in range(3):
                    x0 = x + k * 0.025
                    d.line(self._P(x0, 0.70, x0 - 0.012, 0.74), fill=PINK, width=int(0.008 * u))
        if spec.get("tears"):
            for i, x in enumerate((0.36, 0.64)):
                p = (t + i * 0.5) % 1.0
                y = 0.55 + p * 0.33
                a = 1 - p
                col = _lerp((0, 0, 0), BLUE, a)
                d.ellipse(self._P(x - 0.014, y - 0.02, x + 0.014, y + 0.02), fill=col)
        if spec.get("zzz"):
            for i in range(3):
                p = (t + i / 3) % 1.0
                f = _font(int((0.07 + 0.06 * p) * u))
                col = _lerp((0, 0, 0), CYAN, min(1.0, (1 - p) * 1.4))
                d.text((int((0.78 + 0.10 * p) * u), int((0.26 - 0.22 * p) * u)), "z", fill=col, font=f)
        if spec.get("ringing"):  # "ringing" marks flashing at the top corners
            on = (int(t * spec["frames"]) // 2) % 2 == 0
            col = color if on else _lerp((0, 0, 0), color, 0.35)
            w = max(2, int(0.016 * u))
            for side in (-1, 1):
                cx, cy = 0.5 + side * 0.36, 0.13
                for ang in (-35, 0, 35):
                    a = math.radians(ang)
                    dx_, dy_ = side * math.sin(math.radians(45) + a * side), -math.cos(math.radians(45) + a * side)
                    r0, r1 = 0.035, 0.085 if on else 0.07
                    d.line(self._P(cx + dx_ * r0, cy + dy_ * r0, cx + dx_ * r1, cy + dy_ * r1), fill=col, width=w)
        if spec.get("question"):
            bob = math.sin(t * 2 * math.pi) * 0.02
            d.text((int(0.80 * u), int((0.08 + bob) * u)), "?", fill=CYAN, font=_font(int(0.14 * u)))

        if dx or dy:
            frame = ImageChops.offset(frame, int(dx * u), int(dy * u))
        return frame.resize((self.size, self.size), Image.LANCZOS)

    def _P(self, *v):
        return [int(x * self.u) for x in v]

    # eyes ------------------------------------------------------------------
    def _capsule(self, g, cx, cy, w, h, fill=255):
        h = max(h, 0.012)
        g.rounded_rectangle(self._P(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2),
                            radius=int(min(w, h) / 2 * self.u), fill=fill)

    def _arc(self, g, cx, cy, w, h, up=True, width=0.03):
        pts = []
        for i in range(25):
            a = math.pi * i / 24
            x = cx - w / 2 + w * i / 24
            y = cy - math.sin(a) * h if up else cy + math.sin(a) * h - h
            pts.append((int(x * self.u), int(y * self.u)))
        wpx = int(width * self.u)
        g.line(pts, fill=255, width=wpx, joint="curve")
        r = wpx // 2
        for x, y in (pts[0], pts[-1]):
            g.ellipse([x - r, y - r, x + r, y + r], fill=255)

    def _eyes(self, g, spec, t):
        kind = spec["eyes"]
        L, R, Y = 0.36, 0.64, 0.45
        W, H = 0.12, 0.17
        if kind == "capsule":
            open_ = 1.0
            if spec.get("blink"):
                open_ = 1 - 0.92 * max(_pulse(t, 0.80, 0.07), _pulse(t, 0.90, 0.06))
            shift = math.sin(t * 2 * math.pi) * 0.02 if spec.get("drift") else 0
            for x in (L, R):
                self._capsule(g, x + shift, Y, W, H * open_)
        elif kind == "look_up":
            side = math.sin(t * 2 * math.pi) * 0.03
            for x in (L, R):
                self._capsule(g, x + 0.02 + side, Y - 0.04, W * 0.95, H * 0.55)
        elif kind == "arc":
            for x in (L, R):
                self._arc(g, x, Y + 0.035, W * 1.15, 0.07, up=True, width=0.034)
        elif kind == "closed_happy":
            for x in (L, R):
                self._arc(g, x, Y + 0.02, W * 1.1, 0.035, up=False, width=0.022)
        elif kind == "closed":
            for x in (L, R):
                self._capsule(g, x, Y + 0.03, W * 1.1, 0.022)
        elif kind == "shades":
            g.rounded_rectangle(self._P(0.25, Y - 0.045, 0.75, Y + 0.045), radius=int(0.045 * self.u), fill=210)
            # glint sweeping across the shades
            gx = 0.20 + 0.70 * ((t * 1.0) % 1.0)
            g.polygon(self._P(gx, Y - 0.05, gx + 0.04, Y - 0.05, gx + 0.01, Y + 0.05, gx - 0.03, Y + 0.05), fill=255)
            g.rounded_rectangle(self._P(0.25, Y - 0.045, 0.75, Y + 0.045), radius=int(0.045 * self.u),
                                outline=255, width=int(0.006 * self.u))
        elif kind in ("sad", "angry"):
            for i, x in enumerate((L, R)):
                self._capsule(g, x, Y, W, H * 0.9)
                outer = x - W if i == 0 else x + W
                inner = x + W if i == 0 else x - W
                # lid line: sad = outer side low, angry = inner side low
                if kind == "sad":
                    pts = [(outer, Y - 0.01), (inner, Y - 0.10), (inner, Y - 0.30), (outer, Y - 0.30)]
                else:
                    pts = [(outer, Y - 0.10), (inner, Y - 0.005), (inner, Y - 0.30), (outer, Y - 0.30)]
                g.polygon([(int(px * self.u), int(py * self.u)) for px, py in pts], fill=0)
        elif kind == "round":
            s = 1 + 0.08 * math.sin(t * 2 * math.pi * 2) if spec.get("pop") else 1
            for x in (L, R):
                r = 0.075 * s
                g.ellipse(self._P(x - r, Y - r, x + r, Y + r), fill=255)
                g.ellipse(self._P(x - r * 0.45, Y - r * 0.45, x + r * 0.45, Y + r * 0.45), fill=0)
        elif kind == "heart":
            s = 1 + 0.12 * max(_pulse(t, 0.0, 0.25), _pulse(t, 0.3, 0.25))
            for x in (L, R):
                self._heart(g, x, Y, 0.075 * s)
        elif kind == "wink":
            self._capsule(g, L, Y, W, H)
            self._arc(g, R, Y + 0.035, W * 1.15, 0.07, up=True, width=0.034)
        elif kind == "clock":  # two little clock faces with sweeping hands
            for i, x in enumerate((L, R)):
                r = 0.07
                g.ellipse(self._P(x - r, Y - r, x + r, Y + r), outline=255, width=int(0.018 * self.u))
                a = 2 * math.pi * (t + i * 0.5) - math.pi / 2
                g.line(self._P(x, Y, x + 0.75 * r * math.cos(a), Y + 0.75 * r * math.sin(a)),
                       fill=255, width=int(0.016 * self.u))
                g.ellipse(self._P(x - 0.012, Y - 0.012, x + 0.012, Y + 0.012), fill=255)
        elif kind == "confused":
            self._capsule(g, L, Y + 0.01, W, H * 0.8)
            self._capsule(g, R, Y - 0.02, W * 0.8, H * 0.45)

    def _heart(self, g, cx, cy, r):
        pts = []
        for i in range(60):
            a = 2 * math.pi * i / 60
            x = 16 * math.sin(a) ** 3
            y = -(13 * math.cos(a) - 5 * math.cos(2 * a) - 2 * math.cos(3 * a) - math.cos(4 * a))
            pts.append((int((cx + x / 17 * r) * self.u), int((cy + y / 17 * r) * self.u)))
        g.polygon(pts, fill=255)

    # mouth -----------------------------------------------------------------
    def _mouth(self, e, spec, t):
        kind = spec["mouth"]
        Y = 0.74
        xs = [0.42, 0.46, 0.50, 0.54, 0.58]
        bw = 0.022
        if kind == "dots":
            for x in xs:
                self._capsule(e, x, Y, bw, bw, fill=70)
        elif kind in ("eq", "eq_fast"):
            speed = 6 if kind == "eq_fast" else 3
            for i, x in enumerate(xs):
                h = 0.02 + 0.05 * abs(math.sin(t * 2 * math.pi * speed / 3 + i * 1.3)) * (1 - abs(i - 2) * 0.18)
                self._capsule(e, x, Y, bw, h, fill=200)
        elif kind == "think":
            for i, x in enumerate(xs[1:4]):
                on = 0.3 + 0.7 * max(0.0, math.sin(t * 2 * math.pi - i * 0.9))
                self._capsule(e, x, Y, bw * 1.1, bw * 1.1, fill=int(220 * on))
        elif kind == "frown":
            for x, y in zip(xs, (0.76, 0.745, 0.74, 0.745, 0.76)):
                self._capsule(e, x, y, bw, bw, fill=150)
        elif kind == "flat":
            self._capsule(e, 0.50, Y, 0.16, bw, fill=150)
        elif kind == "o":
            r = 0.028
            e.ellipse(self._P(0.5 - r, Y - r, 0.5 + r, Y + r), outline=200, width=int(0.009 * self.u))


def render_gif(face: Face, mood: str, path: Path, fps: int = 12) -> int:
    n = MOODS[mood]["frames"]
    frames = [face.render(mood, i / n) for i in range(n)]
    # one shared 48-colour palette keeps files small and avoids flicker
    pal = frames[len(frames) // 2].quantize(colors=48, method=Image.Quantize.MEDIANCUT)
    q = [f.quantize(palette=pal, dither=Image.Dither.NONE) for f in frames]
    q[0].save(path, save_all=True, append_images=q[1:], duration=int(1000 / fps),
              loop=0, optimize=False, disposal=1)
    return path.stat().st_size


def main(argv: list[str]) -> None:
    out = Path(argv[1] if len(argv) > 1 else "emoji")
    size = int(argv[argv.index("--size") + 1]) if "--size" in argv else 300
    out.mkdir(parents=True, exist_ok=True)
    face = Face(size)
    total = 0
    for mood in MOODS:
        total += render_gif(face, mood, out / f"{mood}.gif")
    for alias, src in ALIASES.items():
        data = (out / f"{src}.gif").read_bytes()
        (out / f"{alias}.gif").write_bytes(data)
        total += len(data)
    print(f"Wrote {len(MOODS) + len(ALIASES)} GIFs ({total / 1024:.0f} KB) to {out}")


if __name__ == "__main__":
    main(sys.argv)
