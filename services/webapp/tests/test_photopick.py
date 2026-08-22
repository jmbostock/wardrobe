"""Photo-pick tests — best saved photo as the try-on base for a garment
(outfit-match via vision model, pure-PIL fallback). Runnable without pytest:
    python services/webapp/tests/test_photopick.py
"""
from __future__ import annotations

import io
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="altacloset-photopick-test-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import photopick  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _bright(w: int, h: int) -> bytes:
    """Bright, sharp, high-contrast synthetic photo (mini full-body stand-in)."""
    img = Image.new("RGB", (w, h), (210, 210, 210))
    d = ImageDraw.Draw(img)
    for x in range(0, w, 12):
        d.line([(x, 0), (x, h)], fill=(20, 20, 20), width=4)
    return _png(img)


def test_parse_photo_lines():
    text = (
        "Here are my picks:\n"
        "PHOTO 2: 88 - swimsuit base matches\n"
        "PHOTO 1: 45 - long dress, poor for swimsuit\n"
        "PHOTO 3: 20 - blurry"
    )
    assert photopick._parse_photo_lines(text) == [
        (2, 88, "swimsuit base matches"),
        (1, 45, "long dress, poor for swimsuit"),
        (3, 20, "blurry"),
    ]
    # tolerates a bare score + extra prose lines
    assert photopick._parse_photo_lines(
        "PHOTO 1: 92 reason here\nok done"
    ) == [(1, 92, "reason here")]


def _stub(photopick_mod, fn):
    """Temporarily replace photopick._vision_rank and restore after."""
    orig = photopick_mod._vision_rank
    photopick_mod._vision_rank = fn
    return orig


def test_rank_ai_then_fallback_skips_corrupt():
    from app import auth, photos as photos_mod

    ua = auth.create_user("pick@example.com", "password123")
    p_good = photos_mod.upload(ua["id"], _bright(768, 1024), ".png")
    p_bad = photos_mod.upload(ua["id"], _bright(1200, 700), ".png")
    photos_mod.upload(ua["id"], b"corrupt-not-an-image", ".png")  # must be skipped
    garment = _bright(400, 600)

    # --- vision path uses the model's outfit-match scores (deterministic) ---
    orig = _stub(photopick, lambda gb, cands: {
        p_bad["id"]: {"score": 95, "reason": "best outfit match"},
        p_good["id"]: {"score": 40, "reason": "poor match"},
    })
    try:
        ranked = photopick.rank_photos_for_garment(ua["id"], garment, "dress")
    finally:
        photopick._vision_rank = orig
    assert len(ranked) == 2, ranked
    assert ranked[0]["id"] == p_bad["id"] and ranked[0]["method"] == "ai"
    assert ranked[0]["score"] == 95 and ranked[0]["grade"] == "Great"
    assert ranked[0]["reason"] == "best outfit match"
    assert ranked[1]["id"] == p_good["id"]

    # --- fallback: vision unavailable -> pure-PIL heuristic, portrait wins ---
    orig2 = _stub(photopick, lambda gb, cands: None)
    try:
        ranked2 = photopick.rank_photos_for_garment(ua["id"], garment, "dress")
    finally:
        photopick._vision_rank = orig2
    assert len(ranked2) == 2
    assert all(r["method"] == "heuristic" for r in ranked2)
    assert ranked2[0]["id"] == p_good["id"], ranked2


def test_rank_cross_user_isolation():
    from app import auth, photos as photos_mod

    ua = auth.create_user("pickA@example.com", "password123")
    ub = auth.create_user("pickB@example.com", "password123")
    pa = photos_mod.upload(ua["id"], _bright(768, 1024), ".png")
    pb = photos_mod.upload(ub["id"], _bright(1200, 700), ".png")
    garment = _bright(400, 600)
    orig = _stub(photopick, lambda gb, cands: None)  # exercise the fallback path
    try:
        ra = photopick.rank_photos_for_garment(ua["id"], garment, "dress")
        rb = photopick.rank_photos_for_garment(ub["id"], garment, "dress")
    finally:
        photopick._vision_rank = orig
    assert [r["id"] for r in ra] == [pa["id"]]
    assert [r["id"] for r in rb] == [pb["id"]]


def test_rank_fills_vision_gaps():
    from app import auth, photos as photos_mod

    ua = auth.create_user("pickgap@example.com", "password123")
    p1 = photos_mod.upload(ua["id"], _bright(768, 1024), ".png")
    p2 = photos_mod.upload(ua["id"], _bright(640, 1536), ".png")
    garment = _bright(400, 600)
    # vision only ranks p2 — p1 must STILL be ranked (quality heuristic), not dropped
    orig = _stub(photopick, lambda gb, cands: {
        p2["id"]: {"score": 90, "reason": "outfit match"},
    })
    try:
        ranked = photopick.rank_photos_for_garment(ua["id"], garment, "dress")
    finally:
        photopick._vision_rank = orig
    by_id = {r["id"]: r for r in ranked}
    assert len(ranked) == 2, ranked
    assert by_id[p2["id"]]["method"] == "ai" and by_id[p2["id"]]["score"] == 90
    assert by_id[p1["id"]]["method"] == "heuristic" and "score" in by_id[p1["id"]]


def test_route_best_for_garment():
    from fastapi.testclient import TestClient

    from app import auth, media, photos as photos_mod
    from app.main import app
    from app.store import wardrobe

    c = TestClient(app)
    u = auth.create_user("pickroute@example.com", "password123")
    r = c.post(
        "/api/auth/login",
        json={"email": "pickroute@example.com", "password": "password123"},
    )
    h = {"Authorization": "Bearer " + r.json()["token"]}
    photo = photos_mod.upload(u["id"], _bright(768, 1024), ".png")
    g = wardrobe.create(u["id"], "One Piece Swimsuit", "dress")
    gdir = media.WARDROBE_DIR / str(u["id"])
    gdir.mkdir(parents=True, exist_ok=True)
    (gdir / f"{g.id}.png").write_bytes(_bright(400, 600))

    orig = _stub(photopick, lambda gb, cands: {
        photo["id"]: {"score": 91, "reason": "swimsuit base matches"},
    })
    try:
        j = c.get(f"/api/photos/best-for-garment/{g.id}", headers=h).json()
    finally:
        photopick._vision_rank = orig
    assert j["garment_name"] == "One Piece Swimsuit"
    assert j["method"] == "ai"
    assert j["best_id"] == photo["id"] and j["best"]["score"] == 91
    assert len(j["ranked"]) == 1 and j["ranked"][0]["method"] == "ai"
    # auth guard: no token -> 401; unknown garment -> 404
    assert c.get(f"/api/photos/best-for-garment/{g.id}").status_code == 401
    assert c.get("/api/photos/best-for-garment/999999", headers=h).status_code == 404


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
