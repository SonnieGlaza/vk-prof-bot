#!/usr/bin/env python3
"""Рисунок к вопросу 32 КОТ: пять фигур в ряд. Сохраняет PNG в assets/."""

import os

from PIL import Image, ImageDraw, ImageFont

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(_BASE, "assets", "kot_question_32.png")


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    pw, ph, gap = 120, 105, 16
    label_h = 28
    W = 5 * pw + 4 * gap + 40
    H = ph + label_h + 24
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 22)
    except OSError:
        font = ImageFont.load_default()

    y0 = 12

    for i in range(5):
        x0 = 20 + i * (pw + gap)
        y1 = y0 + ph
        xm = x0 + pw // 2
        ym = y0 + ph // 2

        if i == 0:
            # Прямоугольный треугольник: прямой угол справа сверху
            d.polygon([(x0 + 20, y0 + 14), (x0 + 98, y0 + 16), (x0 + 98, y1 - 12)], outline="black", width=2)
        elif i == 1:
            # Прямоугольный треугольник: прямой угол слева сверху (зеркально)
            d.polygon([(x0 + 22, y0 + 16), (x0 + 100, y0 + 14), (x0 + 22, y1 - 12)], outline="black", width=2)
        elif i == 2:
            # Малый квадрат
            s = 34
            d.rectangle([xm - s // 2, ym - s // 2, xm + s // 2, ym + s // 2], outline="black", width=2)
        elif i == 3:
            # Крупный квадрат
            s = 56
            d.rectangle([xm - s // 2, ym - s // 2, xm + s // 2, ym + s // 2], outline="black", width=2)
        else:
            # Г-образная фигура (контур)
            x1, ya = x0 + 18, y0 + 22
            x2, yb = x0 + 102, y1 - 12
            cut = 40
            d.line([(x1, ya), (x2, ya), (x2, yb), (x1 + cut, yb)], fill="black", width=2)
            d.line([(x1 + cut, yb), (x1 + cut, yb - cut), (x1, yb - cut)], fill="black", width=2)
            d.line([(x1, yb - cut), (x1, ya)], fill="black", width=2)

        label = str(i + 1)
        tw = d.textlength(label, font=font) if hasattr(d, "textlength") else 10
        d.text((x0 + (pw - tw) // 2, y1 + 4), label, fill="black", font=font)

    img.save(OUT, "PNG")
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
