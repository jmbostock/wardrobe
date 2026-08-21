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
    u = auth.create_user("alice", "password123")
    assert u["username"] == "alice"
    assert auth.authenticate("alice", "password123")["id"] == u["id"]
    assert auth.authenticate("alice", "wrongpass") is None
    tok = auth.create_session(u["id"])
    assert auth.get_user_by_token(tok)["username"] == "alice"
    auth.delete_session(tok)
    assert auth.get_user_by_token(tok) is None


def test_duplicate_username_rejected():
    auth.create_user("alice2", "password123")  # take the name first
    try:
        auth.create_user("alice2", "anotherpass123")
        raise AssertionError("expected AuthError for duplicate username")
    except auth.AuthError:
        pass


def test_short_password_rejected():
    try:
        auth.create_user("newuser", "short")
        raise AssertionError("expected AuthError for short password")
    except auth.AuthError:
        pass


def test_users_have_isolated_wardrobes():
    ua = auth.create_user("bob", "password123")
    ub = auth.create_user("carol", "password123")
    w = Wardrobe()
    ga, gb = w.all(ua["id"]), w.all(ub["id"])
    assert len(ga) == 25 and len(gb) == 25
    # every garment row belongs to exactly one user
    assert {g.id for g in ga}.isdisjoint({g.id for g in gb})
    # recommend for bob only ever returns bob's garments
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
