"""Family sharing + interaction-log tests (rec-engine v2).

Family group model: one global Family group, every user a member. A garment
owner marks an item "shared to Family"; every other member then sees it (owned
∪ family-shared is the visible set). `user_garment_state.fit_ok` lets a viewer
exclude shared items that don't fit them from recommendations. Interactions
(shown/tried_on/saved/rated/liked/disliked) are logged for the learning engine.
Runnable without pytest:
    python services/webapp/tests/test_sharing.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="altacloset-sharing-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, interactions, recommender, sharing  # noqa: E402
from app.main import app  # noqa: E402
from app.store import wardrobe  # noqa: E402


def _mkuser(c: TestClient, email: str) -> tuple[int, dict]:
    try:
        auth.create_user(email, "password123")
    except auth.AuthError:
        pass
    tok = c.post(
        "/api/auth/login", json={"email": email, "password": "password123"}
    ).json()["token"]
    return auth.get_user_by_token(tok)["id"], {"Authorization": "Bearer " + tok}


def test_family_membership():
    c = TestClient(app)
    uid, _ = _mkuser(c, "fam@example.com")
    assert sharing.user_group_ids(uid), "new user should be in the Family group"


def test_share_visibility_and_owner_only():
    c = TestClient(app)
    uid_a, h_a = _mkuser(c, "owner@example.com")
    uid_b, h_b = _mkuser(c, "viewer@example.com")

    g = wardrobe.create(uid_a, "Shared navy blazer", "outerwear", color_tags="navy")
    r = c.post(f"/api/wardrobe/{g.id}/share", json={"shared": True}, headers=h_a)
    assert r.status_code == 200 and r.json()["shared"] is True

    # viewer sees it as shared; owner-only get() is None, get_visible works
    items_b = wardrobe.all(uid_b)
    shared_b = [x for x in items_b if x.id == g.id]
    assert shared_b and shared_b[0].shared is True
    assert wardrobe.get(uid_b, g.id) is None
    assert wardrobe.get_visible(uid_b, g.id) is not None

    # a non-owner cannot mutate via the share endpoint
    r = c.post(f"/api/wardrobe/{g.id}/share", json={"shared": False}, headers=h_b)
    assert r.status_code == 404
    assert wardrobe.get_visible(uid_b, g.id).shared is True  # still shared


def test_fit_excludes_from_recommend():
    c = TestClient(app)
    uid_a, h_a = _mkuser(c, "fitowner@example.com")
    uid_b, h_b = _mkuser(c, "fitviewer@example.com")

    g = wardrobe.create(uid_a, "Too small jacket", "outerwear", color_tags="black")
    c.post(f"/api/wardrobe/{g.id}/share", json={"shared": True}, headers=h_a)
    # viewer says it doesn't fit
    r = c.post(f"/api/wardrobe/{g.id}/fit", json={"fit_ok": False}, headers=h_b)
    assert r.status_code == 200

    cold = recommender.Weather(temp_c=2.0, condition="clear")  # needs outerwear
    res_b = recommender.recommend(cold, "casual", wardrobe=wardrobe, user_id=uid_b)
    assert res_b["outfit"].get("outerwear") is None, "shared too-small jacket must not be suggested"
    res_a = recommender.recommend(cold, "casual", wardrobe=wardrobe, user_id=uid_a)
    assert res_a["outfit"].get("outerwear") is not None, "owner still gets their own jacket"


def test_interactions_logged():
    c = TestClient(app)
    uid, h = _mkuser(c, "int@example.com")
    g = wardrobe.create(uid, "White tee", "top", color_tags="white")

    # recommend() logs 'shown'
    recommender.recommend(
        recommender.Weather(temp_c=22.0, condition="clear"), "casual",
        wardrobe=wardrobe, user_id=uid,
    )
    kinds = {i["kind"] for i in interactions.recent(uid)}
    assert "shown" in kinds

    # garment rating 7-10 logs 'rated_up'
    r = c.patch(f"/api/wardrobe/{g.id}", json={"rating": 9}, headers=h)
    assert r.status_code == 200
    kinds = {i["kind"] for i in interactions.recent(uid)}
    assert "rated_up" in kinds

    # explicit like/dislike via the feedback endpoint
    for kind in ("liked", "disliked"):
        r = c.post(f"/api/wardrobe/{g.id}/feedback", json={"kind": kind}, headers=h)
        assert r.status_code == 200
    kinds = {i["kind"] for i in interactions.recent(uid)}
    assert "liked" in kinds and "disliked" in kinds

    # unknown feedback kind rejected
    r = c.post(f"/api/wardrobe/{g.id}/feedback", json={"kind": "meh"}, headers=h)
    assert r.status_code == 400


def _main() -> None:
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    _main()
