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
    items = store.list(uid)
    assert len(items) == 1 and items[0]["id"] == o["id"]
    assert store.delete(uid, o["id"]) is True
    assert store.list(uid) == []


def test_cross_user_isolation():
    ua = auth.create_user("oA@example.com", "password123")
    ub = auth.create_user("oB@example.com", "password123")
    g = w.create(ua["id"], "Mine", "top")
    o = store.create(ua["id"], "A look", [g.id])
    assert store.list(ub["id"]) == []
    assert store.delete(ub["id"], o["id"]) is False
    assert len(store.list(ua["id"])) == 1


if __name__ == "__main__":
    test_create_list_delete()
    test_cross_user_isolation()
    print("outfits tests OK")
