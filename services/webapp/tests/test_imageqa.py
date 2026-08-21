"""Image-quality assessment tests — runnable without pytest:
    python services/webapp/tests/test_imageqa.py
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="altacloset-imageqa-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import imageqa  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_person_good_vs_poor():
    good = Image.new("RGB", (400, 800), (200, 200, 200))  # tall, bright, sharp
    d = ImageDraw.Draw(good)
    for x in range(0, 400, 12):
        d.line([(x, 0), (x, 800)], fill=(0, 0, 0), width=4)
    r = imageqa.assess_person(_png(good))
    assert r["score"] >= 70, r

    poor = Image.new("RGB", (200, 200), (5, 5, 5))  # small, square, dark, flat
    r2 = imageqa.assess_person(_png(poor))
    assert r2["score"] <= 60, r2
    assert any("Low resolution" in i for i in r2["issues"]), r2


def test_garment_model_hint():
    img = Image.new("RGB", (400, 400), (240, 240, 240))
    d = ImageDraw.Draw(img)
    d.rectangle([150, 100, 250, 300], fill=(220, 170, 130))  # skin-toned patch
    r = imageqa.assess_garment(_png(img))
    assert any("flat-lay" in i.lower() for i in r["issues"]), r


def test_exif_rotated_portrait_not_penalized():
    # A portrait photo stored rotated (landscape pixels + EXIF orientation=6)
    # must be scored as portrait, not as "nearly square or landscape".
    base = Image.new("RGB", (1000, 500), (200, 200, 200))  # raw landscape pixels
    d = ImageDraw.Draw(base)
    for x in range(0, 1000, 12):
        d.line([(x, 0), (x, 500)], fill=(0, 0, 0), width=4)
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90° CW to display → should read as portrait
    buf = io.BytesIO()
    base.save(buf, "JPEG", exif=exif)
    data = buf.getvalue()
    assert Image.open(io.BytesIO(data)).size == (1000, 500)  # raw is landscape
    r = imageqa.assess_person(data)
    assert r["score"] >= 85, r
    assert not any("landscape" in i.lower() for i in r["issues"]), r


if __name__ == "__main__":
    test_person_good_vs_poor()
    test_garment_model_hint()
    test_exif_rotated_portrait_not_penalized()
    print("imageqa tests OK")
