"""Person-photo module tests — runnable without pytest:
    python services/webapp/tests/test_photos.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="altacloset-photos-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import auth, photos  # noqa: E402


def test_upload_list_default_and_delete():
    ua = auth.create_user("photo1@example.com", "password123")
    p1 = photos.upload(ua["id"], b"fake-jpeg-bytes-1", ".jpg")
    p2 = photos.upload(ua["id"], b"fake-jpeg-bytes-2", ".png")
    items = photos.list(ua["id"])
    assert len(items) == 2
    assert p1["is_default"] is True, "first photo becomes the default"
    assert p2["is_default"] is False
    # make the second the default
    photos.set_default(ua["id"], p2["id"])
    items = photos.list(ua["id"])
    assert next(p["is_default"] for p in items if p["id"] == p2["id"]) is True
    assert next(p["is_default"] for p in items if p["id"] == p1["id"]) is False
    # deleting the default promotes the remaining one
    photos.delete(ua["id"], p2["id"])
    items = photos.list(ua["id"])
    assert len(items) == 1 and items[0]["is_default"] is True
    assert photos.photo_bytes(ua["id"], p1["id"]) == b"fake-jpeg-bytes-1"


def test_cross_user_isolation():
    ua = auth.create_user("photoA@example.com", "password123")
    ub = auth.create_user("photoB@example.com", "password123")
    p = photos.upload(ua["id"], b"aaa", ".jpg")
    try:
        photos.set_default(ub["id"], p["id"])
        raise AssertionError("expected PhotoError for cross-user default")
    except photos.PhotoError:
        pass
    try:
        photos.photo_bytes(ub["id"], p["id"])
        raise AssertionError("expected PhotoError for cross-user read")
    except photos.PhotoError:
        pass


def test_description_roundtrip():
    ua = auth.create_user("photodesc@example.com", "password123")
    p = photos.upload(ua["id"], b"desc-jpeg", ".jpg")
    assert p["description"] == ""
    updated = photos.set_description(ua["id"], p["id"], "front view, summer")
    assert updated["description"] == "front view, summer"
    listed = photos.list(ua["id"])
    assert next(x["description"] for x in listed if x["id"] == p["id"]) == "front view, summer"
    # cross-user guard
    ub = auth.create_user("photodesc2@example.com", "password123")
    try:
        photos.set_description(ub["id"], p["id"], "hijack")
        raise AssertionError("expected PhotoError for cross-user description")
    except photos.PhotoError:
        pass


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL {fn.__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
