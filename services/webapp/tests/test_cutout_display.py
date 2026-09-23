"""De-backgrounded garment display tests — runnable without pytest:
`cd services/webapp && /usr/bin/python3 tests/test_cutout_display.py`

The wardrobe shows a garment's de-backgrounded cutout (`<gid>.cutout.png`)
instead of its flat-lay photo. Two things must hold, and both have been wrong
in production:

  1. The cutout must be served with its ALPHA INTACT. Flattened to RGB, the
     pixels under the transparency leak through — Qwen-Image fills that area
     with a magenta/violet colour (~165,58,208), which painted every wardrobe
     card bright purple.
  2. The cutout must never be mistaken for "the original" image. It is a derived
     artifact; rotate / vision / the render reference all operate on the real
     photo, and the glob `{gid}.*` used to hand them the cutout by accident
     (`.cutout.png` sorts before `.jpg`).
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="cluelesscloset-cutout-test-")
os.environ["DATA_DIR"] = _TMP
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from app import media  # noqa: E402

GID = 4242
UID = 77
# The colour Qwen leaves in the transparent pixels of a cutout.
HIDDEN_BG = (165, 58, 208)


def _write_wardrobe_files() -> tuple[Path, Path]:
    d = Path(_TMP) / "wardrobe" / str(UID)
    d.mkdir(parents=True, exist_ok=True)
    orig = d / f"{GID}.jpg"
    Image.new("RGB", (400, 533), (200, 180, 160)).save(orig, "JPEG")  # flat-lay
    cut = d / f"{GID}{media.CUTOUT_SUFFIX}"
    img = Image.new("RGBA", (400, 533), HIDDEN_BG + (0,))       # transparent
    for x in range(150, 250):
        for y in range(120, 400):
            img.putpixel((x, y), (40, 60, 120, 255))            # the garment
    img.save(cut, "PNG")
    return orig, cut


def _run() -> None:
    orig, cut = _write_wardrobe_files()

    # 1. The cutout is not "the original", and the display path prefers it.
    assert media._is_variant(cut), "cutout must not be treated as the original"
    assert media.garment_image_path(UID, GID) == orig, "original must stay the photo"
    assert media.garment_cutout_path(UID, GID) == cut
    assert media.garment_display_path(UID, GID) == cut, "UI should show the cutout"

    # 2. Thumbnails keep transparency (a flatten would repaint the card purple).
    path, mtype = media.garment_image_file(UID, GID, "thumb")
    assert mtype == "image/webp", mtype
    with Image.open(path) as im:
        assert im.mode in ("RGBA", "LA"), (
            "cutout thumb lost its alpha channel -> the hidden background colour "
            f"would show through (mode {im.mode})")
        corner = im.convert("RGBA").getpixel((2, 2))
        assert corner[3] == 0, f"corner should stay transparent, got {corner}"
        body = im.convert("RGBA").getpixel((im.width // 2, im.height // 2))
        assert body[3] == 255, "garment pixels must stay opaque"
    # detail size too
    path_d, _ = media.garment_image_file(UID, GID, "detail")
    with Image.open(path_d) as im:
        assert im.mode in ("RGBA", "LA"), "detail variant lost its alpha channel"
    # full-size serves the cutout itself (PNG with alpha)
    full, mt_full = media.garment_image_file(UID, GID, "full")
    assert full == cut and mt_full == "image/png", (full, mt_full)

    # 3. A garment with no cutout falls back to the photo, unchanged behaviour.
    orig2 = Path(_TMP) / "wardrobe" / str(UID) / "5555.jpg"
    Image.new("RGB", (400, 533), (10, 10, 10)).save(orig2, "JPEG")
    assert media.garment_display_path(UID, 5555) == orig2
    p2, _ = media.garment_image_file(UID, 5555, "thumb")
    with Image.open(p2) as im:
        assert im.mode == "RGB", "a plain photo should stay RGB (smaller files)"

    # 4. Adding a cutout busts the ?v= cache, so clients refetch the new image.
    v_before = media.garment_image_version(UID, 5555)
    cut2 = Path(_TMP) / "wardrobe" / str(UID) / f"5555{media.CUTOUT_SUFFIX}"
    Image.new("RGBA", (400, 533), (0, 0, 0, 0)).save(cut2, "PNG")
    # the version is whole seconds, so force a distinct mtime instead of sleeping
    os.utime(cut2, (v_before + 90 - media.IMAGE_SERVE_REVISION,) * 2)
    v_after = media.garment_image_version(UID, 5555)
    assert v_after != v_before, "adding a cutout must bump the image version"
    # ...and the revision constant keeps old cached URLs invalid across releases
    assert media.IMAGE_SERVE_REVISION, "serving revision must be non-zero"

    print("test_cutout_display: OK")


if __name__ == "__main__":
    _run()
