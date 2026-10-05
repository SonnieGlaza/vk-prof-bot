#!/usr/bin/env python3
"""Рисунок к вопросу 49 КОТ: пять фигур в ряд (как в методичке). Сохраняет PNG в assets/."""

import os

from PIL import Image, ImageDraw, ImageFont

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(_BASE, "assets", "kot_question_49.png")


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    # Полоса: 5 панелей, подписи 1–5
    pw, ph, gap = 120, 100, 16
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
        xm, ym = x0 + pw // 2, y0 + ph // 2

        if i == 0:
            # Параллелограмм
            d.polygon([(x0 + 15, y0 + 15), (x0 + 95, y0 + 22), (x0 + 105, y1 - 12), (x0 + 25, y1 - 18)], outline="black", width=2)
        elif i == 1:
            # Квадрат
            s = 55
            d.rectangle([xm - s // 2, ym - s // 2, xm + s // 2, ym + s // 2], outline="black", width=2)
        elif i == 2:
            # Треугольник (единственный с тремя сторонами)
            d.polygon([(xm, y0 + 18), (x0 + 18, y1 - 14), (x0 + pw - 18, y1 - 14)], outline="black", width=2)
        elif i == 3:
            # Трапеция / четырёхугольник
            d.polygon([(x0 + 20, y0 + 22), (x0 + 95, y0 + 18), (x0 + 100, y1 - 15), (x0 + 18, y1 - 10)], outline="black", width=2)
        else:
            # Меньший квадрат
            s = 36
            d.rectangle([xm - s // 2, ym - s // 2, xm + s // 2, ym + s // 2], outline="black", width=2)

        label = str(i + 1)
        tw = d.textlength(label, font=font) if hasattr(d, "textlength") else 10
        d.text((x0 + (pw - tw) // 2, y1 + 4), label, fill="black", font=font)

    img.save(OUT, "PNG")
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
