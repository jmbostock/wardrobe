"""Dev accounts (admin + test) tests — runnable without pytest (stdlib only):
    python services/webapp/tests/test_admin.py

Covers the two dev accounts:
  admin  — can act AS any user (live as-if-user session)
  test   — holds a COPY of a real user's data; changes only affect the copy
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="altacloset-admin-test-"))
os.environ["DEV_ADMIN_ENABLED"] = "1"
os.environ["DEV_ADMIN_LOGIN"] = "admin"
os.environ["DEV_ADMIN_PASSWORD"] = "Rimmer256!"
os.environ["DEV_TEST_LOGIN"] = "test"
os.environ["DEV_TEST_PASSWORD"] = "Rimmer256!"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import admin, auth, db, photos  # noqa: E402
from app.outfits import OutfitStore  # noqa: E402
from app.wardrobe import Wardrobe  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
_DATA = os.environ["DATA_DIR"]
_SEED_N = 0


def _seed():
    """One real user with a garment (+image), a person photo, and an outfit."""
    global _SEED_N
    _SEED_N += 1
    u = auth.create_user(f"dana{_SEED_N}@example.com", "password123")
    w = Wardrobe()
    g = w.create(u["id"], "Cream blazer", "top", color_hex="#d9c9a3", color_tags="cream")
    d = Path(_DATA) / "wardrobe" / str(u["id"])
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{g.id}.png").write_bytes(PNG)
    w.update_image(u["id"], g.id, f"{g.id}.png")
    p = photos.upload(u["id"], PNG, ".jpg")
    o = OutfitStore().create(u["id"], "Office look", [g.id], person_photo_id=p["id"])
    return u, w, g, p, o


def test_dev_accounts_created_and_login_by_username():
    auth.ensure_dev_accounts()
    admin_user = auth.authenticate("admin", "Rimmer256!")
    test_user = auth.authenticate("test", "Rimmer256!")
    assert admin_user is not None and admin_user["role"] == "admin"
    assert test_user is not None and test_user["role"] == "test"
    assert auth.authenticate("admin", "wrong-password") is None
    assert auth.authenticate("test", "wrong-password") is None


def test_admin_session_cannot_resolve_to_a_user():
    auth.ensure_dev_accounts()
    tok = auth.create_admin_session()
    assert auth.get_session(tok)["kind"] == "admin"
    assert auth.get_user_by_token(tok) is None


def test_act_as_user_resolves_to_that_user():
    u, w, g, p, o = _seed()
    tok = auth.create_impersonation_session(u["id"])
    resolved = auth.get_user_by_token(tok)
    assert resolved is not None
    assert resolved["id"] == u["id"]                      # acts AS the real user
    assert resolved["session_kind"] == "impersonate"
    assert resolved["role"] == "user"


def test_list_users_excludes_dev_accounts():
    auth.ensure_dev_accounts()
    u, w, g, p, o = _seed()
    emails = {x["email"] for x in admin.list_users()}
    assert u["email"] in emails          # real user listed
    assert "admin@dev.local" not in emails
    assert "test@dev.local" not in emails


def test_copy_into_test_is_separate():
    u, w, g, p, o = _seed()
    auth.ensure_dev_accounts()
    info = admin.copy_into_test(u["id"])
    assert info["from_email"] == u["email"]

    test = auth.get_dev_user("test")
    tg = db.init().execute(
        "SELECT * FROM garments WHERE user_id=?", (test["id"],)
    ).fetchone()
    assert tg is not None and tg["name"] == "Cream blazer" and tg["id"] != g.id
    # image file copied into test's dir
    assert any((Path(_DATA) / "wardrobe" / str(test["id"])).glob(f"{tg['id']}.*"))

    # original untouched
    assert w.get(u["id"], g.id) is not None
    assert db.init().execute(
        "SELECT COUNT(*) FROM garments WHERE user_id=?", (u["id"],)
    ).fetchone()[0] == 1


def test_test_sandbox_changes_never_touch_real_account():
    u, w, g, p, o = _seed()
    auth.ensure_dev_accounts()
    admin.copy_into_test(u["id"])
    test = auth.get_dev_user("test")
    tg = db.init().execute(
        "SELECT id FROM garments WHERE user_id=?", (test["id"],)
    ).fetchone()

    # change the copied garment's rating + add a brand-new garment on test
    w.update(test["id"], tg["id"], rating=9)
    newg = w.create(test["id"], "Test-added tee", "top")

    assert w.get(u["id"], g.id).rating == 0          # real user's rating untouched
    assert w.get(test["id"], tg["id"]).rating == 9   # copy got the change
    assert w.get(u["id"], newg.id) is None           # real user has no new garment
    assert w.get(test["id"], newg.id) is not None    # test does


def test_refresh_test_copy_replaces_old_data():
    u, w, g, p, o = _seed()
    auth.ensure_dev_accounts()
    admin.copy_into_test(u["id"])
    # add a garment directly to test, then refresh → it's replaced by the copy
    test = auth.get_dev_user("test")
    w.create(test["id"], "Extra on test", "top")
    assert db.init().execute(
        "SELECT COUNT(*) FROM garments WHERE user_id=?", (test["id"],)
    ).fetchone()[0] == 2

    admin.copy_into_test(u["id"])
    rows = db.init().execute(
        "SELECT * FROM garments WHERE user_id=?", (test["id"],)
    ).fetchall()
    assert len(rows) == 1 and rows[0]["name"] == "Cream blazer"


def test_test_copy_info_and_seed():
    auth.ensure_dev_accounts()
    _seed()  # guarantee at least one real user exists to copy from
    test = auth.get_dev_user("test")
    # empty the test sandbox regardless of what earlier tests left behind
    conn = db.init()
    with conn:
        for t in ("garments", "photos", "outfits", "clips", "chat_sessions", "interactions"):
            conn.execute(f"DELETE FROM {t} WHERE user_id=?", (test["id"],))

    info = admin.test_copy_info()
    assert info["exists"] and info["counts"]["garments"] == 0

    seeded = admin.ensure_test_copy()  # auto-seeds from the first real user
    assert seeded is not None
    info = admin.test_copy_info()
    assert info["counts"]["garments"] == 1


def test_test_login_gets_a_normal_user_session():
    auth.ensure_dev_accounts()
    test = auth.get_dev_user("test")
    tok = auth.create_session(test["id"])
    resolved = auth.get_user_by_token(tok)
    assert resolved is not None and resolved["id"] == test["id"]
    assert resolved["role"] == "test"


def _run_all():
    failures = 0
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception:
            import traceback

            failures += 1
            print(f"FAIL  {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    _run_all()
