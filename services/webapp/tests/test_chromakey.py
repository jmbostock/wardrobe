"""Green-screen (chroma-key) detection + removal tests — runnable without
pytest: `/usr/bin/python3 tests/test_chromakey.py`."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cluelesscloset-chromakey-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image  # noqa: E402

from app import chromakey  # noqa: E402


def _green_screen_subject() -> Image.Image:
    """A green background with a non-green (brown) figure in the middle — the
    synthetic stand-in for a full-body shot on a green screen."""
    img = Image.new("RGB", (400, 600), (0, 200, 80))  # green screen
    for x in range(160, 240):
        for y in range(120, 480):
            img.putpixel((x, y), (120, 90, 60))  # subject (brown)
    return img


def _run() -> None:
    # 1. Detection: a green-screen frame is recognised...
    gs = _green_screen_subject()
    assert chromakey.is_green_screen(gs) is True, "green screen not detected"

    # 2. Keying: the green background becomes neutral gray, the subject survives.
    out = chromakey.chroma_key(gs)
    assert out.size == gs.size
    # corners/edges were pure green -> now the neutral letterbox gray
    assert out.getpixel((5, 5)) == (128, 128, 128), out.getpixel((5, 5))
    assert out.getpixel((395, 5)) == (128, 128, 128), out.getpixel((395, 5))
    # the subject is preserved (not swallowed by the key)
    assert out.getpixel((200, 300)) == (120, 90, 60), out.getpixel((200, 300))

    # 3. A non-green photo is a no-op (safe for ordinary bases).
    plain = Image.new("RGB", (400, 600), (200, 200, 200))
    assert chromakey.is_green_screen(plain) is False
    same = chromakey.chroma_key(plain)
    assert same.getpixel((5, 5)) == (200, 200, 200)
    assert same.tobytes() == plain.tobytes()

    # NOTE (2026-09-23): the _prep_person / _largest_skin_centroid_y assertions
    # that used to live here tested the CatVTON 768x1024 letterbox + face-up
    # orientation fix. That whole step is gone — the Qwen renderer edits the
    # photo at its native aspect, so there is no crop to neutralise and no
    # orientation heuristic to test.

    print("test_chromakey: OK")


if __name__ == "__main__":
    _run()
