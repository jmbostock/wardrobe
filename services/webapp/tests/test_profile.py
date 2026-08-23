"""Style profile tests: normalize/derive logic + API contract.

The Account "Style profile (optional)" bio feeds a hidden `derived_profile` that
the recommendation engine and stylist consume — it must be persisted server-side
and available to response-building, but never returned to the browser. Runnable
without pytest:
    python services/webapp/tests/test_profile.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="altacloset-profile-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app import auth, db, profile  # noqa: E402
from app.main import app  # noqa: E402


def test_normalize_profile():
    p = profile.normalize_profile(
        {
            "sex": "F", "height": " 5'10\" ", "warmth_bias": "runs cold",
            "formality_min": "SMART-CASUAL", "never_wear": "shorts, yellow, shorts",
            "style_keywords": "minimal, minimal", "bogus": "dropped",
        }
    )
    assert p["sex"] == "f"
    assert p["height"] == "5'10\""
    assert p["warmth_bias"] == "1"          # +1 = runs cold
    assert p["formality_min"] == "smart-casual"
    assert p["never_wear"] == "shorts, yellow"  # csv deduped
    assert p["style_keywords"] == "minimal"
    assert "bogus" not in p

    # bad values normalize to blank
    assert profile.normalize_profile({"sex": "X"})["sex"] == ""
    assert profile.normalize_profile({"warmth_bias": "runs hot"})["warmth_bias"] == "-1"
    assert profile.normalize_profile({"formality_max": "tuxedo"})["formality_max"] == ""


def test_height_to_cm():
    assert profile.height_to_cm("178cm") == 178.0
    assert profile.height_to_cm("5'10\"") == 177.8
    assert abs(profile.height_to_cm("68") - 172.7) < 0.1   # bare number = inches
    assert profile.height_to_cm("") is None
    assert profile.height_to_cm(None) is None


def test_derive_profile():
    d = profile.derive_profile(
        {
            "sex": "m", "height": "6'0\"", "top_size": "L",
            "bottom_size": "34W x 32L", "shoe_size": "11",
            "warmth_bias": "-1", "formality_min": "casual", "formality_max": "formal",
            "never_wear": "shorts, plaid, yellow",
            "style_keywords": "minimal, preppy", "occasions": "office 4x/wk, gym 3x/wk",
            "fav_colors": "navy, gray", "colors_avoid": "orange",
        }
    )
    assert d["warmth_bias"] == -1
    assert d["size_buckets"] == {"top": "l", "waist_in": 34, "shoe": "11"}
    assert d["guardrails"] == ["no_shorts", "no_patterns", "avoid_color:yellow"]
    assert d["style_tags"] == ["minimal", "preppy"]
    assert d["occasion_weights"] == {"office": 4.0, "gym": 3.0}
    assert d["formality_zone"] == {"min": "casual", "max": "formal", "range": 3}
    assert d["palette"] == {"fav": ["navy", "gray"], "avoid": ["orange"]}
    assert d["completeness"] >= 0.8
    assert d["version"] == 1


def test_api_profile_save_and_exposed():
    c = TestClient(app)
    try:
        auth.create_user("pf@example.com", "password123")
    except auth.AuthError:
        pass
    tok = c.post(
        "/api/auth/login", json={"email": "pf@example.com", "password": "password123"}
    ).json()["token"]
    h = {"Authorization": "Bearer " + tok}

    r = c.post(
        "/api/account/profile",
        json={"profile": {"sex": "f", "height": "5'4\"", "warmth_bias": "runs cold",
                          "never_wear": "shorts, patterns"}},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["profile"]["warmth_bias"] == "1"

    # derived profile persisted server-side (also available to stylist/recommender)
    uid = auth.get_user_by_token(tok)["id"]
    derived = profile.load_derived(uid)
    assert derived["warmth_bias"] == 1
    assert derived["guardrails"] == ["no_shorts", "no_patterns"]

    # ...and exposed everywhere a user/account dict is returned
    for path in ("/api/account", "/api/auth/me"):
        body = c.get(path, headers=h).json()
        assert "derived_profile" in body.get("user", {}), f"{path} user"
        assert body["user"]["derived_profile"]["warmth_bias"] == 1
    # /api/account also exposes it at top level alongside the bio
    acc = c.get("/api/account", headers=h).json()
    assert acc["derived_profile"]["warmth_bias"] == 1
    assert acc["derived_profile"]["guardrails"] == ["no_shorts", "no_patterns"]
    assert acc["profile"]["sex"] == "f"


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
