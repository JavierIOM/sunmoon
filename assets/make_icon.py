"""Draws assets/sunmoon.ico, sunmoon.png and sunmoon.icns: an orange sun
mid-eclipse behind a dark moon, inside a pointing reticle. Needs Pillow, which
only the Windows build uses; the Mac build takes the committed sunmoon.icns.

    python assets/make_icon.py
"""

import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
S = 1024
BG, RING, TICK = "#121417", "#3a4048", "#5f6770"
SUN, MOON = "#F56400", "#2a2f36"


def draw():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, S - 1, S - 1), radius=int(S * 0.22), fill=BG)
    c = S / 2
    r = S * 0.36
    w = int(S * 0.03)
    d.ellipse((c - r, c - r, c + r, c + r), outline=RING, width=w)
    for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
        a, b = S * 0.40, S * 0.47
        d.line((c + dx * a, c + dy * a, c + dx * b, c + dy * b), fill=TICK, width=w)
    rs = S * 0.25
    d.ellipse((c - rs, c - rs, c + rs, c + rs), fill=SUN)
    mx, my, rm = c + S * 0.10, c - S * 0.075, S * 0.215
    d.ellipse((mx - rm, my - rm, mx + rm, my + rm), fill=MOON)
    return img


def main():
    big = draw()
    big.resize((256, 256), Image.LANCZOS).save(os.path.join(HERE, "sunmoon.png"))
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    big.resize((256, 256), Image.LANCZOS).save(
        os.path.join(HERE, "sunmoon.ico"), format="ICO", sizes=[(n, n) for n in sizes])
    # macOS icons sit inside a 1024 canvas with a 100 px margin, so it lines up with
    # the rest of the Dock rather than looking a size too big
    mac = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    mac.paste(big.resize((824, 824), Image.LANCZOS), (100, 100))
    mac.save(os.path.join(HERE, "sunmoon.icns"), format="ICNS")
    print("wrote sunmoon.ico, sunmoon.png and sunmoon.icns")


if __name__ == "__main__":
    main()
