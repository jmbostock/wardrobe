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
# are usually ≤ 6 bits; the same item at a slightly different angle/hang can be
# up to ~16. 12 is a good "worth flagging" line.
SIMILAR_THRESHOLD = 12


def image_phash(data: bytes) -> str:
    """64-bit dHash of an image's bytes, as 16 lowercase hex chars."""
    img = Image.open(io.BytesIO(data)).convert("L")
    img = img.resize((9, 8), Image.LANCZOS)
    px = list(img.getdata())
    bits: list[str] = []
    for y in range(8):
        row = y * 9
        for x in range(8):
            bits.append("1" if px[row + x] > px[row + x + 1] else "0")
    return hex(int("".join(bits), 2))[2:].zfill(16)


def hamming(a: str, b: str) -> int:
    """Number of differing bits between two hex phashes (0 = identical)."""
    return sum(x != y for x, y in zip(a, b))
