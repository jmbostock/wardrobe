"""Saved-outfits tests — runnable without pytest:
    python services/webapp/tests/test_outfits.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="altacloset-outfits-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import auth, outfits as outfits_mod, wardrobe  # noqa: E402

store = outfits_mod.OutfitStore()
w = wardrobe.Wardrobe()


def test_create_list_delete():
    ua = auth.create_user("o1@example.com", "password123")
    uid = ua["id"]
    t = w.create(uid, "Tee", "top")
    b = w.create(uid, "Pants", "bottom")
    assert store.list(uid) == []
    o = store.create(uid, "My look", [t.id, b.id], result_url="/api/uploads/x.png")
    assert o["name"] == "My look" and o["garment_ids"] == [t.id, b.id]
    assert o["result_url"] == "/api/uploads/x.png"
    assert o["rating"] == 0
    items = store.list(uid)
    assert len(items) == 1 and items[0]["id"] == o["id"]
    assert store.delete(uid, o["id"]) is True
    assert store.list(uid) == []


def test_generated_ref_id():
    """Every saved outfit gets a short, unique reference id (e.g. O-9F3K2M)
    so a render can be cited in chat."""
    ua = auth.create_user("oRef@example.com", "password123")
    uid = ua["id"]
    g = w.create(uid, "Tee", "top")
    o1 = store.create(uid, "Look A", [g.id])
    o2 = store.create(uid, "Look B", [g.id])
    assert o1["ref_id"] and o1["ref_id"].startswith("O-"), o1["ref_id"]
    assert o2["ref_id"] and o2["ref_id"] != o1["ref_id"]
    # persisted + returned on get/list
    assert store.get(uid, o1["id"])["ref_id"] == o1["ref_id"]
    assert {o["ref_id"] for o in store.list(uid)} == {o1["ref_id"], o2["ref_id"]}


def test_create_stores_person_mapping():
    """Outfits record which source person photo produced the render (metadata
    only — no image copies)."""
    ua = auth.create_user("oMap@example.com", "password123")
    uid = ua["id"]
    g = w.create(uid, "Tee", "top")
    o = store.create(uid, "Look", [g.id], result_url="/api/uploads/r.png",
                     person_photo_id=7, person_url="/api/photos/7/image")
    assert o["person_photo_id"] == 7
    assert o["person_url"] == "/api/photos/7/image"
    # defaults when not supplied
    o2 = store.create(uid, "Look2", [g.id], result_url="/api/uploads/r2.png")
    assert o2["person_photo_id"] == 0 and o2["person_url"] == ""


def test_update():
    ua = auth.create_user("oU@example.com", "password123")
    uid = ua["id"]
    g = w.create(uid, "Tee", "top")
    o = store.create(uid, "My look", [g.id])
    assert o["rating"] == 0
    assert store.update(uid, o["id"], name="Better look", rating=9) is True
    o2 = store.get(uid, o["id"])
    assert o2 is not None and o2["name"] == "Better look" and o2["rating"] == 9
    # rating-only update keeps the name
    assert store.update(uid, o["id"], rating=6) is True
    assert store.get(uid, o["id"])["name"] == "Better look"
    assert store.get(uid, o["id"])["rating"] == 6
    # cross-user isolation + no-op
    ub = auth.create_user("oU2@example.com", "password123")
    assert store.update(ub["id"], o["id"], rating=1) is False
    assert store.get(ub["id"], o["id"]) is None
    assert store.update(uid, o["id"]) is False


def test_cross_user_isolation():
    ua = auth.create_user("oA@example.com", "password123")
    ub = auth.create_user("oB@example.com", "password123")
    g = w.create(ua["id"], "Mine", "top")
    o = store.create(ua["id"], "A look", [g.id])


def test_clips_store():
    from app.clips import ClipStore

    cs = ClipStore()
    ua = auth.create_user("oC@example.com", "password123")
    c = cs.create(ua["id"], "prompt-123", outfit_id=7)
    assert c["status"] == "queued" and c["prompt_id"] == "prompt-123"
    assert c["outfit_id"] == 7
    assert cs.update(ua["id"], c["id"], status="done", result_url="/api/uploads/x.webp") is True
    c2 = cs.get(ua["id"], c["id"])
    assert c2 is not None and c2["status"] == "done" and c2["result_url"] == "/api/uploads/x.webp"
    # cross-user isolation
    ub = auth.create_user("oC2@example.com", "password123")
    assert cs.get(ub["id"], c["id"]) is None
    assert cs.update(ub["id"], c["id"], status="error") is False


def test_clips_latest_by_outfit():
    from app.clips import ClipStore

    cs = ClipStore()
    ua = auth.create_user("oC3@example.com", "password123")
    c1 = cs.create(ua["id"], "prompt-a", outfit_id=42)
    c2 = cs.create(ua["id"], "prompt-b", outfit_id=42)
    latest = cs.latest_by_outfit(ua["id"], 42)
    assert latest is not None and latest["id"] == c2["id"]
    assert cs.latest_by_outfit(ua["id"], 999) is None
    # cross-user isolation
    ub = auth.create_user("oC4@example.com", "password123")
    assert cs.latest_by_outfit(ub["id"], 42) is None


def test_svd_letterbox():
    from app.svd import _letterbox
    from PIL import Image
    import io as _io

    # a tall portrait still
    buf = _io.BytesIO()
    Image.new("RGB", (400, 900), (200, 200, 200)).save(buf, "PNG")
    out = _letterbox(buf.getvalue())
    img = Image.open(_io.BytesIO(out))
    assert img.size == (576, 1024), img.size
    assert store.list(ub["id"]) == []
    assert store.delete(ub["id"], o["id"]) is False
    assert len(store.list(ua["id"])) == 1


if __name__ == "__main__":
    test_create_list_delete()
    test_update()
    test_cross_user_isolation()
    print("outfits tests OK")
