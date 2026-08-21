"""Wardrobe create/image/delete module tests — runnable without pytest:
    python services/webapp/tests/test_wardrobe.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="altacloset-wardrobe-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import auth, wardrobe  # noqa: E402
from app.tryon import _load_garment_image  # noqa: E402

w = wardrobe.Wardrobe()

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64  # png magic + padding


def test_create_upload_serve_delete():
    ua = auth.create_user("w1@example.com", "password123")
    g = w.create(ua["id"], "Test tee", "top", color_hex="#aabbcc", color_tags="gray")
    assert g.id > 0 and g.name == "Test tee" and g.category == "top"

    # save an image for the garment (mirrors main._save_garment_image)
    data_dir = os.environ["DATA_DIR"]
    d = Path(data_dir) / "wardrobe" / str(ua["id"])
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{g.id}.png"
    p.write_bytes(PNG)
    assert w.update_image(ua["id"], g.id, p.name) is True

    # tryon loader finds it via glob (in-memory g has no image_path yet)
    assert _load_garment_image(g, ua["id"]) == PNG
    # ...and via recorded image_path after reload
    g2 = w.get(ua["id"], g.id)
    assert g2 is not None and g2.image_path == p.name
    assert _load_garment_image(g2, ua["id"]) == PNG

    assert w.delete(ua["id"], g.id) is True
    assert w.get(ua["id"], g.id) is None


def test_cross_user_isolation():
    ua = auth.create_user("wA@example.com", "password123")
    ub = auth.create_user("wB@example.com", "password123")
    g = w.create(ua["id"], "Mine", "top")
    assert w.get(ub["id"], g.id) is None
    assert w.update_image(ub["id"], g.id, "x.png") is False
    assert w.delete(ub["id"], g.id) is False
    assert w.get(ua["id"], g.id) is not None


if __name__ == "__main__":
    test_create_upload_serve_delete()
    test_cross_user_isolation()
    print("wardrobe tests OK")
