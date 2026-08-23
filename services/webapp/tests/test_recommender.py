"""Recommender tests — runnable without pytest:
    python services/webapp/tests/test_recommender.py
No generic seed wardrobe anymore (2026-08-21), so each test builds an explicit
wardrobe via Wardrobe.create().
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# MUST be set before importing app.* — app.config reads env at import time
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="altacloset-test-"))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import auth, wardrobe as wardrobe_mod  # noqa: E402
from app.recommender import Weather, recommend  # noqa: E402

_COUNTER = [0]


def _make_wardrobe() -> tuple[wardrobe_mod.Wardrobe, int]:
    """A user with a small but varied wardrobe (all recommender traits covered)."""
    _COUNTER[0] += 1
    w = wardrobe_mod.Wardrobe()
    uid = auth.create_user(f"reco{_COUNTER[0]}@example.com", "password123")["id"]
    mk = lambda name, cat, **kw: w.create(uid, name, cat, **kw)  # noqa: E731
    mk("Business oxford", "top", warmth=2, formality="business", occasions="office", material="cotton")
    mk("Cotton tee", "top", warmth=1, formality="casual", occasions="casual,active", material="cotton")
    mk("Wool crewneck", "top", warmth=4, formality="smart-casual", occasions="office,date,casual", material="wool")
    mk("Navy dress", "dress", warmth=3, formality="formal", occasions="event,office,date")
    mk("Chinos", "bottom", warmth=3, formality="business", occasions="office,date", material="cotton")
    mk("Jeans", "bottom", warmth=3, formality="casual", occasions="casual,active,date", material="denim")
    mk("Waterproof shell", "outerwear", warmth=2, waterproof=1, formality="smart-casual", occasions="casual,active,office", material="nylon")
    mk("Wool overcoat", "outerwear", warmth=5, waterproof=0, formality="formal", occasions="office,event,date", material="wool")
    mk("Sneakers", "footwear", warmth=1, formality="casual", occasions="casual,active,date")
    mk("Sun hat", "accessory", formality="casual", occasions="active,beach", material="straw")
    mk("Wool beanie", "accessory", formality="casual", occasions="casual,active", material="wool")
    mk("Leather belt", "accessory", formality="all", occasions="office,date,event", material="leather")
    return w, uid


def _pick(outfit, role):
    return outfit[role]


def test_rainy_office_gets_waterproof():
    w, uid = _make_wardrobe()
    out = recommend(
        Weather(temp_c=13, feels_like_c=12, condition="rain", wind_kph=20),
        "office",
        wardrobe=w,
        user_id=uid,
    )["outfit"]
    outer = _pick(out, "outerwear")
    assert outer is not None and outer["waterproof"] == 1, f"expected waterproof outer, got {outer}"
    assert out["top"]["formality"] in ("business", "smart-casual")


def test_hot_beach_is_light():
    w, uid = _make_wardrobe()
    out = recommend(
        Weather(temp_c=30, feels_like_c=31, condition="clear", uv_index=9),
        "beach",
        wardrobe=w,
        user_id=uid,
    )["outfit"]
    assert out["top"]["warmth"] <= 2
    assert out["bottom"]["category"] == "bottom"
    assert any("sun hat" in a["name"].lower() for a in out["accessories"])


def test_swimsuit_for_hot_beach_and_no_bottom():
    """A swimsuit is a hot-weather one-piece: recommended for beach, never for
    the office, and never paired with a separate bottom."""
    _COUNTER[0] += 1
    w = wardrobe_mod.Wardrobe()
    uid = auth.create_user(f"reco_swim{_COUNTER[0]}@example.com", "password123")["id"]
    w.create(uid, "Navy one-piece swimsuit", "swimsuit", warmth=1, formality="casual", occasions="active,beach")
    w.create(uid, "Cotton tee", "top", warmth=1, formality="casual", occasions="casual,active", material="cotton")
    w.create(uid, "Linen shorts", "bottom", warmth=1, formality="casual", occasions="casual,active,beach", material="cotton")

    out = recommend(
        Weather(temp_c=31, feels_like_c=33, condition="clear", uv_index=10),
        "beach", wardrobe=w, user_id=uid,
    )["outfit"]
    assert out["top"] is not None and out["top"]["category"] == "swimsuit", out["top"]
    assert out["bottom"] is None, "swimsuit is a one-piece — no separate bottom"

    out2 = recommend(
        Weather(temp_c=18, feels_like_c=17, condition="clear"),
        "office", wardrobe=w, user_id=uid,
    )["outfit"]
    assert out2["top"]["category"] != "swimsuit", "swimsuit should not be recommended for the office"


def test_cold_hiking_layers():
    w, uid = _make_wardrobe()
    out = recommend(
        Weather(temp_c=8, feels_like_c=6, condition="cloudy", wind_kph=15),
        "hiking",
        wardrobe=w,
        user_id=uid,
    )["outfit"]
    assert out["outerwear"] is not None, "cold hike should layer an outerwear piece"


def test_cold_day_layers_lighter_top_under_jacket():
    """On a cold day with a jacket available, prefer a lighter top (the jacket
    adds the warmth) instead of a heavy top AND a heavy jacket."""
    _COUNTER[0] += 1
    w = wardrobe_mod.Wardrobe()
    uid = auth.create_user(f"reco_layer{_COUNTER[0]}@example.com", "password123")["id"]
    mk = lambda name, cat, **kw: w.create(uid, name, cat, **kw)  # noqa: E731
    mk("Light tee", "top", warmth=2, formality="casual", occasions="casual")
    mk("Heavy sweater", "top", warmth=5, formality="casual", occasions="casual")
    mk("Denim jacket", "outerwear", warmth=3, formality="casual", occasions="casual")
    mk("Jeans", "bottom", warmth=3, formality="casual", occasions="casual")
    out = recommend(
        Weather(temp_c=2, feels_like_c=0), "casual", wardrobe=w, user_id=uid,
    )["outfit"]
    assert out["outerwear"] is not None, "cold day should include a jacket"
    assert out["top"]["warmth"] <= 3, f"expected a lighter top under the jacket, got {out['top']}"


def test_formal_picks_dress_and_no_bottom():
    w, uid = _make_wardrobe()
    out = recommend(
        Weather(temp_c=14, feels_like_c=13, condition="cloudy"),
        "formal",
        prompt="navy",
        wardrobe=w,
        user_id=uid,
    )["outfit"]
    assert out["top"]["category"] == "dress"
    assert out["bottom"] is None, "a dress covers the bottom slot"


def test_reasoning_is_explainable():
    w, uid = _make_wardrobe()
    res = recommend(
        Weather(temp_c=13, feels_like_c=12, condition="rain"),
        "office",
        prompt="navy",
        wardrobe=w,
        user_id=uid,
    )
    assert len(res["reasoning"]) >= 2
    assert res["weather_used"]["temp_f"] is not None


def test_empty_wardrobe_returns_helpful_note():
    w = wardrobe_mod.Wardrobe()
    uid = auth.create_user("empty@example.com", "password123")["id"]
    res = recommend(Weather(temp_c=20, feels_like_c=20), "casual", wardrobe=w, user_id=uid)
    assert res["note"] == "empty_wardrobe"
    assert res["outfit"] == {}


def test_owned_only_excludes_wishlist_items():
    w = wardrobe_mod.Wardrobe()
    uid = auth.create_user("reco-own@example.com", "password123")["id"]
    mk = lambda name, cat, **kw: w.create(uid, name, cat, **kw)  # noqa: E731
    top_biz = mk("Business oxford", "top", warmth=2, formality="business", occasions="office")
    top_wool = mk("Wool crewneck", "top", warmth=4, formality="smart-casual", occasions="office,date,casual")
    overcoat = mk("Wool overcoat", "outerwear", warmth=5, waterproof=0, formality="formal", occasions="office")
    chinos = mk("Chinos", "bottom", warmth=3, formality="business", occasions="office")
    shoes = mk("Sneakers", "footwear", warmth=1, formality="casual", occasions="casual")
    # mark the best-scoring candidates as to-buy (not owned)
    w.update(uid, top_biz.id, owned=0)
    w.update(uid, overcoat.id, owned=0)
    out = recommend(
        Weather(temp_c=2, feels_like_c=0),
        "office",
        wardrobe=w,
        user_id=uid,
        owned_only=True,
    )["outfit"]
    # owned-only must skip the to-buy oxford + overcoat
    assert out["top"]["id"] == top_wool.id, out
    assert out["outerwear"] is None, out  # only overcoat (to-buy) was cold enough
    # full recommendation may still pick the to-buy overcoat
    full = recommend(
        Weather(temp_c=2, feels_like_c=0), "office", wardrobe=w, user_id=uid
    )["outfit"]
    assert full["top"] is not None
    # nothing owned-only-excluded leaks into the owned result
    for role in ("top", "bottom", "outerwear", "footwear"):
        g = out.get(role)
        assert g is None or g["owned"] == 1, f"{role} leaked a wishlist item: {g}"


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
