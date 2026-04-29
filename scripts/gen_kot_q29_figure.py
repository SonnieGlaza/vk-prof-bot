#!/usr/bin/env python3
"""Рисунок к вопросу 29 КОТ: многоугольник с точками 1–14 по контуру."""

import math
import os

from PIL import Image, ImageDraw, ImageFont

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(_BASE, "assets", "kot_question_29.png")


def _tick(draw, ax, ay, bx, by, t, inward=True):
    """Перпендикулярная засечка к отрезку AB на расстоянии t от A."""
    lax, lay = bx - ax, by - ay
    ln = math.hypot(lax, lay) or 1.0
    ux, uy = lax / ln, lay / ln
    mx, my = ax + ux * t, ay + uy * t
    px, py = -uy, ux
    if not inward:
        px, py = -px, -py
    h = 11
    draw.line([(mx - px * h, my - py * h), (mx + px * h, my + py * h)], fill="black", width=2)


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    W, H = 920, 620
    img = Image.new("RGB", (W, H), "white")
    dr = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 20)
        font_sm = ImageFont.truetype("DejaVuSans.ttf", 15)
    except OSError:
        font = font_sm = ImageFont.load_default()

    # Контур: 5 → 6 → … → 4 → 5 (по часовой стрелке по описанию методички)
    pts = {
        5: (460, 70),
        6: (548, 110),
        7: (628, 168),
        8: (708, 248),
        9: (738, 328),
        10: (708, 498),
        11: (598, 428),
        12: (498, 488),
        13: (388, 408),
        14: (252, 498),
        1: (188, 388),
        2: (248, 288),
        3: (308, 218),
        4: (378, 142),
    }
    order = [5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 1, 2, 3, 4]
    poly = [pts[i] for i in order]
    dr.polygon(poly, outline="black", width=3)

    # Засечки на скатах и боковых участках
    _tick(dr, pts[5][0], pts[5][1], pts[4][0], pts[4][1], t=42)
    _tick(dr, pts[5][0], pts[5][1], pts[4][0], pts[4][1], t=105)
    _tick(dr, pts[5][0], pts[5][1], pts[4][0], pts[4][1], t=168)
    _tick(dr, pts[5][0], pts[5][1], pts[8][0], pts[8][1], t=58, inward=False)
    _tick(dr, pts[5][0], pts[5][1], pts[8][0], pts[8][1], t=128, inward=False)
    _tick(dr, pts[8][0], pts[8][1], pts[10][0], pts[10][1], t=95)

    cx = sum(x for x, _ in poly) / len(poly)
    cy = sum(y for _, y in poly) / len(poly)
    for num in order:
        x, y = pts[num]
        vx, vy = x - cx, y - cy
        vlen = math.hypot(vx, vy) or 1.0
        ox, oy = vx / vlen * 24, vy / vlen * 24
        s = str(num)
        tw = dr.textlength(s, font=font) if hasattr(dr, "textlength") else 12
        dr.text((x + ox - tw / 2, y + oy - 10), s, fill="black", font=font)

    dr.text(
        (24, 14),
        "Раздели фигуру одной прямой через две точки (см. текст вопроса в чате).",
        fill="gray",
        font=font_sm,
    )
    img.save(OUT, "PNG")
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
