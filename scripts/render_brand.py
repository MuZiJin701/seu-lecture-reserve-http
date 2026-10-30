"""Render the text-free book/clock logo and the local static landscape (Pillow only)."""

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


def render(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    scale = 4
    logo = Image.new('RGBA', (256 * scale, 256 * scale))
    draw = ImageDraw.Draw(logo)
    def points(values: list[tuple[int, int]]) -> list[tuple[int, int]]:
        return [(x * scale, y * scale) for x, y in values]
    ink = '#314b3b'
    draw.polygon(points([(30, 101), (80, 85), (126, 103), (172, 85), (222, 101),
                         (222, 210), (172, 194), (126, 211), (80, 194), (30, 210)]), fill='#f4f5e7')
    draw.line(points([(30, 101), (80, 85), (126, 103), (172, 85), (222, 101),
                      (222, 210), (172, 194), (126, 211), (80, 194), (30, 210), (30, 101)]), fill=ink, width=7*scale, joint='curve')
    draw.line(points([(126, 105), (126, 208)]), fill=ink, width=5*scale)
    for offset in (0, 22, 44):
        draw.line(points([(48, 124+offset), (80, 114+offset), (106, 124+offset)]), fill='#91a28b', width=4*scale, joint='curve')
        draw.line(points([(146, 124+offset), (172, 114+offset), (204, 124+offset)]), fill='#91a28b', width=4*scale, joint='curve')
    draw.ellipse((141*scale, 27*scale, 233*scale, 119*scale), fill='#f4f5e7', outline=ink, width=7*scale)
    draw.line(points([(187, 45), (187, 73), (206, 84)]), fill='#a26c45', width=6*scale, joint='curve')
    logo = logo.resize((256, 256), Image.Resampling.LANCZOS)
    assert logo.mode == 'RGBA' and logo.getbbox() is not None and logo.getpixel((0, 0))[3] == 0
    logo.save(directory / 'logo.png', optimize=True)

    width, height = 1600, 1100
    landscape = Image.new('RGB', (width, height))
    draw = ImageDraw.Draw(landscape)
    for y in range(height):
        fraction = y / height
        color = tuple(round(a + (b-a)*fraction) for a, b in zip((245, 244, 224), (171, 190, 158)))
        draw.line((0, y, width, y), fill=color)
    draw.ellipse((1110, 145, 1320, 355), fill='#f9f3d7')
    for level, color in [(410, '#c2d0b9'), (560, '#a9bea2'), (715, '#8fa986'), (895, '#77946f')]:
        ridge = [(x, int(level + 65*math.sin(x/350 + level/200) + 31*math.cos(x/140))) for x in range(0, width+1, 5)]
        draw.polygon([*ridge, (width, height), (0, height)], fill=color)
    landscape = landscape.filter(ImageFilter.GaussianBlur(3))
    assert landscape.size == (1600, 1100)
    landscape.save(directory / 'landscape.png', optimize=True)


if __name__ == '__main__':
    render(Path(__file__).resolve().parents[1] / 'seu_reserve' / 'web_assets')
