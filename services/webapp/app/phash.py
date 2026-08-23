"""Perceptual hash (dHash) for garment near-duplicate detection.

dHash (difference hash) turns an image into a 64-bit fingerprint where
*similar* images produce *similar* hashes — closeness is measured by Hamming
distance. Two photos of the same garment taken the same way land within a few
bits; unrelated garments differ by ~30+ bits. This lets us flag near-duplicates
when adding/importing wardrobe photos ("didn't I already scan this?").

Pure Pillow, no ML. 64-bit hash → 16 hex chars.
"""
from __future__ import annotations

import io

from PIL import Image

# Hamming distance below which we call two garments "similar". Real duplicates
# (same garment re-photographed) are usually <= 6 bits on the center-crop hash;
# the same item at a slightly different angle/hang can be up to ~8. We hash the
# CENTER CROP (see image_phash), so unrelated garments land well above this.
SIMILAR_THRESHOLD = 8
# Distances between SIMILAR_THRESHOLD and DEBATE_THRESHOLD are the "debate"
# zone — plausibly the same item re-photographed (different angle/lighting) but
# not provably so. These are flagged as 'possible duplicate' instead of blocked.
DEBATE_THRESHOLD = 20


def image_phash(data: bytes, crop_frac: float = 0.6) -> str:
    """64-bit dHash of a garment image's CENTER CROP, as 16 lowercase hex chars.

    We hash the center crop — not the whole frame — because garment photos
    (flat-lays especially) are dominated by the background (bed/sheet), so a
    whole-frame dHash makes every similarly-shot garment look "similar" (a red
    one-piece and a pink polka-dot swimsuit were matching at dist 11). The
    garment is centered, so the center crop captures it — the same region the
    color gate uses."""
    img = Image.open(io.BytesIO(data)).convert("L")
    w, h = img.size
    cw, ch = max(1, int(w * crop_frac)), max(1, int(h * crop_frac))
    left, top = (w - cw) // 2, (h - ch) // 2
    img = img.crop((left, top, left + cw, top + ch))
    img = img.resize((9, 8), Image.LANCZOS)
    px = list(img.getdata())
    bits: list[str] = []
    for y in range(8):
        row = y * 9
        for x in range(8):
            bits.append("1" if px[row + x] > px[row + x + 1] else "0")
    return hex(int("".join(bits), 2))[2:].zfill(16)


def hamming(a: str | int, b: str | int) -> int:
    """Number of differing bits (0 to 64) between two 64-bit phashes.
    Accepts 16-char hex strings or integer bitfields."""
    if not a or not b:
        return 64
    try:
        val_a = int(a, 16) if isinstance(a, str) else int(a)
        val_b = int(b, 16) if isinstance(b, str) else int(b)
        return (val_a ^ val_b).bit_count()
    except Exception:  # noqa: BLE001
        return 64


# ---- coarse color fingerprint ----------------------------------------------
# dHash alone can't tell an olive-green pair of joggers from a black swimsuit
# when they're photographed the same way (distances land in the same band). So
# we also record a coarse dominant-color class and require it to MATCH before
# calling two garments near-duplicates. Different-colored items never match.

# canonical coarse classes we emit
_COLOR_CLASSES = {
    "black", "white", "gray", "red", "orange", "yellow", "green", "teal",
    "blue", "purple", "pink", "brown",
}


def _pixel_class(r: int, g: int, b: int) -> str:
    mx, mn = max(r, g, b), min(r, g, b)
    d = mx - mn
    v = mx / 255.0
    if d < 40:  # near-achromatic
        if v < 0.25:
            return "black"
        if v > 0.82:
            return "white"
        return "gray"
    rr, gg, bb = r / 255.0, g / 255.0, b / 255.0
    if mx == r:
        h = 60 * (((gg - bb) / d) % 6)
    elif mx == g:
        h = 60 * (((bb - rr) / d) + 2)
    else:
        h = 60 * (((rr - gg) / d) + 4)
    sat = d / mx
    if h <= 15 or h > 345:
        return "red"
    if h <= 45:
        return "orange" if sat > 0.55 else "brown"
    if h <= 70:
        return "yellow"
    if h <= 160:
        return "green"
    if h <= 200:
        return "teal"
    if h <= 260:
        return "blue"
    if h <= 320:
        return "purple"
    return "pink"


def image_color_class(data: bytes, crop_frac: float = 0.6) -> str:
    """Dominant coarse color class of the CENTER of an image ('black', 'blue',
    'green', ...). Empty string if the image can't be read.

    We classify only the central crop: garment photos (hanger / flat-lay /
    product shots) have the garment centered, so the whole-image dominant color
    is usually the BACKGROUND (white sheet / studio backdrop). The center crop
    captures the garment itself, which is what the near-dup gate needs."""
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
        w, h = img.size
        cw, ch = max(1, int(w * crop_frac)), max(1, int(h * crop_frac))
        left, top = (w - cw) // 2, (h - ch) // 2
        img = img.crop((left, top, left + cw, top + ch))
        img.thumbnail((64, 64))
        counts: dict[str, int] = {}
        for r, g, b in img.getdata():
            c = _pixel_class(r, g, b)
            counts[c] = counts.get(c, 0) + 1
        best = max(counts.items(), key=lambda kv: kv[1])
        return best[0] if best[1] >= 2 else ""
    except Exception:  # noqa: BLE001 — unreadable → no fingerprint
        return ""
