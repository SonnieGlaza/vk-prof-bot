#!/usr/bin/env python3
"""Рисунок к вопросу 17 КОТ: пять фигур в ряд (тонкий/толстый крест, шестиугольник, линза, трапеция)."""

import math
import os

from PIL import Image, ImageDraw, ImageFont

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(_BASE, "assets", "kot_question_17.png")


def draw_thin_cross(d, cx, cy, arm, w):
    d.line([(cx - arm, cy), (cx + arm, cy)], fill="black", width=w)
    d.line([(cx, cy - arm), (cx, cy + arm)], fill="black", width=w)


def draw_hex_flat_top(d, cx, cy, r):
    pts = []
    for k in range(6):
        ang = math.radians(-90 + k * 60)
        pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    d.polygon(pts, outline="black", width=3)


def draw_vertical_lens(d, cx, cy):
    """Вертикальная «миндаль»: узкий эллипс."""
    d.ellipse([cx - 22, cy - 48, cx + 22, cy + 48], outline="black", width=3)


def draw_rt_trapezoid(d, cx, cy):
    """Прямоугольная трапеция: справа вертикальный катет, низ длиннее верха."""
    y_t, y_b = cy - 30, cy + 30
    x_r = cx + 26
    x_tl = cx - 12
    x_tr = x_r
    x_bl = cx - 36
    x_br = x_r
    d.polygon([(x_tl, y_t), (x_tr, y_t), (x_br, y_b), (x_bl, y_b)], outline="black", width=3)


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    W, H = 840, 190
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 22)
    except OSError:
        font = ImageFont.load_default()

    panels = [(94, 95), (246, 95), (398, 95), (550, 95), (702, 95)]
    arm = 32

    draw_thin_cross(d, panels[0][0], panels[0][1], arm, 2)
    draw_thin_cross(d, panels[1][0], panels[1][1], arm, 9)
    draw_hex_flat_top(d, panels[2][0], panels[2][1], 38)
    draw_vertical_lens(d, panels[3][0], panels[3][1])
    draw_rt_trapezoid(d, panels[4][0], panels[4][1])

    for i, (cx, cy) in enumerate(panels, start=1):
        s = str(i)
        tw = d.textlength(s, font=font) if hasattr(d, "textlength") else 12
        d.text((cx - tw / 2, cy - 8), s, fill="dimgray", font=font)

    img.save(OUT, "PNG")
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
