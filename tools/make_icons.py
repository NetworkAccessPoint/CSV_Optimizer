#!/usr/bin/env python3
"""Turn the source artwork into the icon files the builds need.

    python tools/make_icons.py [source.webp]

Writes packaging/icons/csvopt.ico (Windows), csvopt.icns (macOS) and
icon.png, plus the favicons the web UI serves. The artwork is kept in the
repository so the icons can be rebuilt rather than treated as binaries that
arrived from nowhere.

Needs Pillow; nothing else in csvopt does, so this is a build-time tool and
not part of the application.
"""

from __future__ import annotations

import os
import sys

try:
    from PIL import Image, ImageDraw, ImageFilter
except ImportError:  # pragma: no cover - a build-time dependency
    sys.exit("Pillow이 필요합니다: python -m pip install pillow")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICONS = os.path.join(ROOT, "packaging", "icons")
WEB = os.path.join(ROOT, "csvopt", "web")
SIZE = 1024
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]
FAVICONS = [16, 32, 180]


def body_box(img: Image.Image) -> tuple[int, int, int, int]:
    """The rounded square itself, without the white page or its drop shadow.

    The shadow is grey and the icon is blue, so the two are told apart by
    colour; a plain "not white" test would drag the shadow in and leave the
    artwork sitting off-centre.
    """
    width, height = img.size
    px = img.load()
    blue = lambda c: c[2] > c[0] + 12
    xs = [x for x in range(width) if blue(px[x, height // 2])]
    ys = [y for y in range(height) if blue(px[width // 2, y])]
    if not xs or not ys:
        return (0, 0, width, height)
    return (xs[0], ys[0], xs[-1] + 1, ys[-1] + 1)


def corner_radius(img: Image.Image, box: tuple[int, int, int, int]) -> int:
    """Measured off the straight run along the top edge."""
    px = img.load()
    blue = lambda c: c[2] > c[0] + 12
    flat = [x for x in range(img.size[0]) if blue(px[x, box[1] + 2])]
    if len(flat) < 2:
        return round(SIZE * 0.16)
    radius = ((box[2] - box[0]) - (flat[-1] - flat[0])) / 2
    return max(1, round(radius * SIZE / (box[2] - box[0])))


def build(source: str) -> None:
    os.makedirs(ICONS, exist_ok=True)
    img = Image.open(source).convert("RGB")
    box = body_box(img)
    radius = corner_radius(img, box)
    icon = img.crop(box).resize((SIZE, SIZE), Image.LANCZOS).convert("RGBA")

    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, SIZE - 1, SIZE - 1), radius=radius, fill=255)
    icon.putalpha(mask.filter(ImageFilter.GaussianBlur(1.1)))

    def scaled(n: int) -> Image.Image:
        out = icon.resize((n, n), Image.LANCZOS)
        if n <= 64:
            # Detail this fine turns to mush at tray sizes; a little edge
            # contrast keeps the sheet and the pencil apart.
            out = out.filter(ImageFilter.UnsharpMask(radius=1.0, percent=90, threshold=2))
        return out

    icon.save(os.path.join(ICONS, "icon.png"))
    icon.save(os.path.join(ICONS, "csvopt.ico"), format="ICO",
              sizes=[(n, n) for n in ICO_SIZES])
    icon.save(os.path.join(ICONS, "csvopt.icns"), format="ICNS")
    for n in FAVICONS:
        scaled(n).save(os.path.join(WEB, f"icon-{n}.png"))
    scaled(32).save(os.path.join(WEB, "favicon.ico"), format="ICO",
                    sizes=[(16, 16), (32, 32)])
    print(f"본체 {box} · 모서리 반지름 {radius}")
    print("packaging/icons/: icon.png, csvopt.ico, csvopt.icns")
    print("csvopt/web/: favicon.ico, " + ", ".join(f"icon-{n}.png" for n in FAVICONS))


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ICONS, "source.webp"))
