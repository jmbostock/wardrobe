"""Generate PWA icons for Clueless Closet (dark rounded tile + accent hanger).

Run:  python scripts/gen-pwa-icons.py
Writes: services/webapp/app/static/icons/{icon-192,icon-512,icon-512-maskable,apple-touch-icon}.png
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parents[1] / "services/webapp/app/static/icons"

BG = (15, 18, 22, 255)        # #0f1216
TILE = (24, 29, 36, 255)      # #181d24
ACCENT = (79, 156, 249, 255)  # #4f9cf9


def rounded(size: int, radius: int, color: tuple) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=color)
    return img


def hanger(d: ImageDraw.ImageDraw, cx: float, top: float, scale: float) -> None:
    """Draw a simple clothes-hanger centered at cx, starting at top, scaled."""
    r = 22 * scale
    # hook
    d.ellipse([cx - r, top, cx + r, top + 2 * r], outline=ACCENT, width=max(3, int(10 * scale)))
    # shoulder bar
    y = top + 3 * r
    d.rounded_rectangle(
        [cx - 150 * scale, y, cx + 150 * scale, y + 26 * scale], radius=12 * scale, fill=ACCENT
    )
    # body triangle
    d.polygon(
        [(cx - 130 * scale, y + 26 * scale), (cx + 130 * scale, y + 26 * scale), (cx, y + 220 * scale)],
        fill=ACCENT,
    )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    def build(size: int, maskable: bool) -> Image.Image:
        tile = rounded(size, size // 8, TILE)
        d = ImageDraw.Draw(tile)
        margin = size * (0.36 if maskable else 0.24)  # maskable keeps content in the safe zone
        scale = size / 512
        hanger(d, size / 2, size * margin, scale * 1.15)
        return tile

    for size in (192, 512):
        build(size, False).save(OUT / f"icon-{size}.png")
    build(512, True).save(OUT / "icon-512-maskable.png")
    # apple touch icon needs an opaque background (iOS ignores alpha)
    build(180, False).convert("RGB").save(OUT / "apple-touch-icon.png")
    print("wrote icons to", OUT)


if __name__ == "__main__":
    main()
