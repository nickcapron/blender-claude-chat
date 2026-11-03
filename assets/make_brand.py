"""Generates crender's brand images: the mark, a README banner and a social thumbnail.

The mark is a "c" built from isometric cubes sweeping around an arc (creaper's family of
colors, teal -> violet -> pink), with a glowing orange "selected vertex" in its opening.
Run with Pillow installed:

    python assets/make_brand.py

Writes SVGs (crisp at any size, used in the README) and PNGs (for video/social).
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(__file__).parent / "brand"

BG = (14, 17, 23)
BG_LIFT = (24, 30, 42)
TEAL = (46, 230, 197)
VIOLET = (139, 92, 246)
PINK = (244, 114, 182)
ORANGE = (255, 158, 64)
TEXT = (236, 240, 246)
MUTED = (139, 148, 163)

FONT_BOLD = "C:/Windows/Fonts/segoeuib.ttf"
FONT_REGULAR = "C:/Windows/Fonts/segoeui.ttf"
SVG_FONT = "'Segoe UI', Inter, 'Helvetica Neue', Arial, sans-serif"

NAME = "crender"
TAGLINE = "Claude, right inside Blender"


def lerp(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def gradient(t: float) -> tuple:
    return lerp(TEAL, VIOLET, t * 2) if t < 0.5 else lerp(VIOLET, PINK, (t - 0.5) * 2)


def shade(c: tuple, k: float) -> tuple:
    return tuple(max(0, min(255, round(v * k))) for v in c)


def hexcolor(c: tuple) -> str:
    return "#%02X%02X%02X" % c


def cubes(cx: float, cy: float, radius: float, scale: float) -> list[tuple]:
    """(faces, ...) for each cube along the arc, ordered back to front.
    faces: [(points, color), ...] for top, left and right faces."""
    count = 18
    start, end = math.radians(48), math.radians(312)  # opening on the right
    out = []
    for i in range(count):
        t = i / (count - 1)
        angle = start + (end - start) * t
        taper = 0.55 + 0.45 * math.sin(math.pi * (0.1 + 0.8 * t))
        s = 33 * scale * taper  # cube "radius"
        x = cx + math.cos(angle) * radius
        y = cy - math.sin(angle) * radius
        base = gradient(t)
        k = 0.866 * s
        top = [(x, y - s), (x + k, y - s / 2), (x, y), (x - k, y - s / 2)]
        left = [(x - k, y - s / 2), (x, y), (x, y + s), (x - k, y + s / 2)]
        right = [(x, y), (x + k, y - s / 2), (x + k, y + s / 2), (x, y + s)]
        faces = [(top, shade(lerp(base, (255, 255, 255), 0.25), 1.0)), (left, shade(base, 0.82)), (right, shade(base, 0.6))]
        out.append((y, faces))
    out.sort(key=lambda c: c[0])
    return [faces for _, faces in out]


def vertex(cx: float, cy: float, radius: float, scale: float) -> tuple:
    return cx + radius, cy, 14 * scale


# ----------------------------------------------------------------------- SVG


def svg_mark(cx: float, cy: float, radius: float, scale: float) -> str:
    parts = []
    for faces in cubes(cx, cy, radius, scale):
        for pts, color in faces:
            p = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
            parts.append(f'<polygon points="{p}" fill="{hexcolor(color)}" stroke="{hexcolor(shade(color, 0.7))}" stroke-width="{1.2 * scale:.1f}" stroke-linejoin="round"/>')
    vx, vy, r = vertex(cx, cy, radius, scale)
    parts.append(f'<circle cx="{vx:.1f}" cy="{vy:.1f}" r="{r * 2.8:.1f}" fill="url(#halo)"/>')
    parts.append(f'<circle cx="{vx:.1f}" cy="{vy:.1f}" r="{r:.1f}" fill="{hexcolor(ORANGE)}"/>')
    return "\n  ".join(parts)


SVG_DEFS = f"""<defs>
    <radialGradient id="bg" cx="35%" cy="40%" r="80%">
      <stop offset="0" stop-color="{hexcolor(BG_LIFT)}"/><stop offset="1" stop-color="{hexcolor(BG)}"/>
    </radialGradient>
    <radialGradient id="halo">
      <stop offset="0" stop-color="{hexcolor(ORANGE)}" stop-opacity="0.6"/>
      <stop offset="1" stop-color="{hexcolor(ORANGE)}" stop-opacity="0"/>
    </radialGradient>
  </defs>"""


def write_svgs() -> None:
    icon = f"""<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 512 512">
  {SVG_DEFS}
  <rect width="512" height="512" rx="112" fill="url(#bg)"/>
  {svg_mark(240, 262, 148, 1.0)}
</svg>
"""
    (OUT / f"{NAME}-icon.svg").write_text(icon, encoding="utf-8")
    banner = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="320" viewBox="0 0 1280 320">
  {SVG_DEFS}
  <rect width="1280" height="320" rx="28" fill="url(#bg)"/>
  {svg_mark(192, 166, 94, 0.64)}
  <text x="350" y="178" font-family="{SVG_FONT}" font-size="112" font-weight="700" fill="{hexcolor(TEXT)}" letter-spacing="-2">{NAME}</text>
  <text x="354" y="232" font-family="{SVG_FONT}" font-size="30" fill="{hexcolor(MUTED)}">{TAGLINE}</text>
</svg>
"""
    (OUT / f"{NAME}-banner.svg").write_text(banner, encoding="utf-8")


# ----------------------------------------------------------------------- PNG

SS = 4  # supersampling for smooth edges


def background(w: int, h: int, radius: int) -> Image.Image:
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    glow = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(glow)
    for i in range(40, 0, -1):
        t = i / 40
        d.ellipse([w * 0.35 - w * t, h * 0.4 - h * t * 1.2, w * 0.35 + w * t, h * 0.4 + h * t * 1.2],
                  fill=lerp(BG_LIFT, BG, t))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=255)
    img.paste(glow, (0, 0), mask)
    return img


def draw_mark(img: Image.Image, cx: float, cy: float, radius: float, scale: float) -> None:
    d = ImageDraw.Draw(img)
    for faces in cubes(cx, cy, radius, scale):
        for pts, color in faces:
            d.polygon(pts, fill=color, outline=shade(color, 0.7), width=max(1, round(1.2 * scale)))
    vx, vy, r = vertex(cx, cy, radius, scale)
    halo = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(halo).ellipse([vx - r * 2.3, vy - r * 2.3, vx + r * 2.3, vy + r * 2.3], fill=ORANGE + (160,))
    halo = halo.filter(ImageFilter.GaussianBlur(r * 1.2))
    img.alpha_composite(halo)
    ImageDraw.Draw(img).ellipse([vx - r, vy - r, vx + r, vy + r], fill=ORANGE)


def text(img: Image.Image, xy: tuple, s: str, size: int, color: tuple, bold: bool = False) -> None:
    font = ImageFont.truetype(FONT_BOLD if bold else FONT_REGULAR, size)
    ImageDraw.Draw(img).text(xy, s, font=font, fill=color, anchor="ls")


def save(img: Image.Image, name: str, w: int, h: int) -> None:
    img.resize((w, h), Image.LANCZOS).save(OUT / name)


def write_pngs() -> None:
    s = SS
    icon = background(512 * s, 512 * s, 112 * s)
    draw_mark(icon, 240 * s, 262 * s, 148 * s, 1.0 * s)
    save(icon, f"{NAME}-icon.png", 512, 512)

    banner = background(1280 * s, 320 * s, 28 * s)
    draw_mark(banner, 192 * s, 166 * s, 94 * s, 0.64 * s)
    text(banner, (350 * s, 178 * s), NAME, 112 * s, TEXT, bold=True)
    text(banner, (354 * s, 232 * s), TAGLINE, 30 * s, MUTED)
    save(banner, f"{NAME}-banner.png", 1280, 320)

    thumb = background(1280 * s, 720 * s, 0)
    draw_mark(thumb, 330 * s, 360 * s, 190 * s, 1.25 * s)
    text(thumb, (600 * s, 360 * s), NAME, 150 * s, TEXT, bold=True)
    text(thumb, (606 * s, 440 * s), TAGLINE, 44 * s, MUTED)
    save(thumb, f"{NAME}-thumbnail.png", 1280, 720)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    write_svgs()
    write_pngs()
    print("wrote", sorted(p.name for p in OUT.iterdir()))
