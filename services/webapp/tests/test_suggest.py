"""Suggest → chat consolidation tests: POST /api/suggest runs the rule-based
recommendation and posts it INTO the stylist chat thread as a rich `recommend`
message (garment cards + reasoning), and re-suggesting appends to the SAME
thread. Runnable without pytest:
    python services/webapp/tests/test_suggest.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="altacloset-suggest-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app import auth  # noqa: E402
from app.main import app  # noqa: E402
from app.store import wardrobe  # noqa: E402

OFFICE_WEATHER = {
    "temp_c": 21.0, "feels_like_c": 21.0, "condition": "clear",
    "wind_kph": 5.0, "humidity": 40, "uv_index": 5.0,
}


def _login(c: TestClient, email: str = "suggest@example.com") -> tuple[dict, int]:
    # tests in this module share one DATA_DIR, so the user may already exist
    try:
        auth.create_user(email, "password123")
    except auth.AuthError:
        pass
    r = c.post("/api/auth/login", json={"email": email, "password": "password123"})
    h = {"Authorization": "Bearer " + r.json()["token"]}
    return h, auth.get_user_by_token(r.json()["token"])["id"]


def _seed_wardrobe(uid: int) -> None:
    wardrobe.create(uid, "Navy Blazer", "outerwear", color_hex="#1a2b4a",
                    color_tags="navy", warmth=3, formality="business", occasions="office")
    wardrobe.create(uid, "White Oxford", "top", color_hex="#f2f2f2",
                    color_tags="white", warmth=2, formality="business", occasions="office")
    wardrobe.create(uid, "Grey Trousers", "bottom", color_hex="#555555",
                    color_tags="gray", warmth=3, formality="business", occasions="office")
    wardrobe.create(uid, "Black Loafers", "footwear", color_hex="#111111",
                    color_tags="black", warmth=3, formality="business", occasions="office")


def test_suggest_posts_recommendation_into_chat():
    c = TestClient(app)
    h, uid = _login(c)
    _seed_wardrobe(uid)

    j = c.post("/api/suggest", headers=h, json={
        "activity": "office", "prompt": "navy", "owned_only": False,
        "weather": OFFICE_WEATHER,
    }).json()

    assert j["session_id"], "must create a chat session"
    assert j["intro"], "Cher's intro text"
    assert j["outfit"]["top"]["name"] == "White Oxford"
    assert j["reasoning"], "deterministic why-this-outfit list"
    assert j["weather_used"]["condition"] == "clear"
    assert len(j["messages"]) == 1

    # the recommendation is persisted as a rich `recommend` message
    hist = c.get(f"/api/recommend/chat/{j['session_id']}", headers=h).json()
    assert len(hist["messages"]) == 1
    msg = hist["messages"][0]
    assert msg["role"] == "assistant" and msg["kind"] == "recommend"
    assert msg["data"]["outfit"]["top"]["id"] == j["outfit"]["top"]["id"]
    assert msg["data"]["reasoning"] == j["reasoning"]


def test_re_suggest_appends_to_same_thread():
    c = TestClient(app)
    h, uid = _login(c)
    _seed_wardrobe(uid)

    j1 = c.post("/api/suggest", headers=h, json={
        "activity": "office", "owned_only": False, "weather": OFFICE_WEATHER,
    }).json()
    j2 = c.post("/api/suggest", headers=h, json={
        "session_id": j1["session_id"], "activity": "casual",
        "owned_only": False, "weather": OFFICE_WEATHER,
    }).json()

    assert j2["session_id"] == j1["session_id"], "re-suggest continues the same thread"
    hist = c.get(f"/api/recommend/chat/{j2['session_id']}", headers=h).json()
    assert len(hist["messages"]) == 2
    assert all(m["kind"] == "recommend" for m in hist["messages"])
    # context now reflects the latest suggestion
    assert hist["context"]["activity"] == "casual"


def test_suggest_defaults_to_owned_only_and_flags_wishlist():
    c = TestClient(app)
    h, uid = _login(c, "ownfilter@example.com")
    # wishlist top scores higher (matches target warmth) than the owned top
    wardrobe.create(uid, "Owned Warm Top", "top", warmth=4, formality="business",
                    occasions="office", owned=1)
    wardrobe.create(uid, "Wishlist Perfect Top", "top", warmth=2, formality="business",
                    occasions="office", owned=0)
    wardrobe.create(uid, "Owned Bottom", "bottom", warmth=3, formality="business",
                    occasions="office", owned=1)

    # default (owned_only omitted -> True): only owned items recommended
    j = c.post("/api/suggest", headers=h, json={
        "activity": "office", "weather": OFFICE_WEATHER,
    }).json()
    assert j["outfit"]["top"]["name"] == "Owned Warm Top", "default should be owned-only"

    # explicit owned_only=false: wishlist item can win and is flagged in the message
    j2 = c.post("/api/suggest", headers=h, json={
        "activity": "office", "owned_only": False, "weather": OFFICE_WEATHER,
    }).json()
    assert j2["outfit"]["top"]["name"] == "Wishlist Perfect Top"
    assert "wishlist" in j2["intro"].lower()
    assert "Wishlist Perfect Top" in j2["intro"]


def test_suggest_auth_guard():
    c = TestClient(app)
    assert c.post("/api/suggest", json={"activity": "casual"}).status_code == 401


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
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
