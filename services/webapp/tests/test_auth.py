"""Auth + per-user isolation tests — runnable without pytest:
    python services/webapp/tests/test_auth.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="altacloset-auth-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import auth  # noqa: E402
from app.recommender import Weather, recommend  # noqa: E402
from app.wardrobe import Wardrobe  # noqa: E402


def test_register_login_session_roundtrip():
    u = auth.create_user("alice@example.com", "password123")
    assert u["email"] == "alice@example.com"
    assert auth.authenticate("alice@example.com", "password123")["id"] == u["id"]
    assert auth.authenticate("Alice@Example.com", "password123")["id"] == u["id"]  # case-insensitive
    assert auth.authenticate("alice@example.com", "wrongpass") is None
    tok = auth.create_session(u["id"])
    assert auth.get_user_by_token(tok)["email"] == "alice@example.com"
    auth.delete_session(tok)
    assert auth.get_user_by_token(tok) is None


def test_duplicate_email_rejected():
    auth.create_user("alice2@example.com", "password123")
    try:
        auth.create_user("alice2@example.com", "anotherpass123")
        raise AssertionError("expected AuthError for duplicate email")
    except auth.AuthError:
        pass


def test_invalid_email_rejected():
    for bad in ("not-an-email", "no-at-sign", "@example.com"):
        try:
            auth.create_user(bad, "password123")
            raise AssertionError(f"expected AuthError for {bad!r}")
        except auth.AuthError:
            pass


def test_short_password_rejected():
    try:
        auth.create_user("new@example.com", "short")
        raise AssertionError("expected AuthError for short password")
    except auth.AuthError:
        pass


def test_change_password():
    u = auth.create_user("pw@example.com", "password123")
    auth.change_password(u["id"], "password123", "newpass456")
    assert auth.authenticate("pw@example.com", "newpass456") is not None
    assert auth.authenticate("pw@example.com", "password123") is None
    try:
        auth.change_password(u["id"], "wrong", "another456")
        raise AssertionError("expected AuthError for wrong current password")
    except auth.AuthError:
        pass


def test_set_location():
    u = auth.create_user("loc@example.com", "password123")
    assert u["lat"] is None  # default location applies at fetch time
    auth.set_location(u["id"], 37.5396, -122.2974)
    assert auth.get_user(u["id"])["lat"] == 37.5396


def test_users_have_isolated_wardrobes():
    ua = auth.create_user("bob@example.com", "password123")
    ub = auth.create_user("carol@example.com", "password123")
    w = Wardrobe()
    ga, gb = w.all(ua["id"]), w.all(ub["id"])
    assert len(ga) == 25 and len(gb) == 25
    assert {g.id for g in ga}.isdisjoint({g.id for g in gb})
    res = recommend(
        Weather(temp_c=13, feels_like_c=12, condition="rain"),
        "office",
        wardrobe=w,
        user_id=ua["id"],
    )
    for val in res["outfit"].values():
        if isinstance(val, dict) and "user_id" in val:
            assert val["user_id"] == ua["id"], val["name"]
        if isinstance(val, list):
            for g in val:
                assert g["user_id"] == ua["id"], g["name"]


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
