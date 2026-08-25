"""Multi-model try-on tests (dev model selection + queue gating).

Runnable without pytest:
    python services/webapp/tests/test_tryon_models.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="altacloset-tryon-models-test-"))
os.environ.setdefault("TRYON_MODELS", "catvton,idm_vton,flux_kontext")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import tryon  # noqa: E402
from app.routes.tryon_routes import _is_dev_session, _resolve_models  # noqa: E402


def test_available_models_includes_catvton():
    avail = tryon.available_models()
    assert "catvton" in avail, avail  # the shipped default is always available
    # idm_vton / flux_kontext are workflow-gated: not available until their
    # workflow JSON ships (so the live default never breaks).
    assert tryon.MODEL_LABELS["idm_vton"]
    assert tryon.MODEL_LABELS["flux_kontext"]


def test_is_dev_session():
    assert _is_dev_session({"role": "admin"}) is True
    assert _is_dev_session({"role": "test"}) is True
    assert _is_dev_session({"role": "user", "session_kind": "impersonate"}) is True
    assert _is_dev_session({"role": "user", "session_kind": "user"}) is False
    assert _is_dev_session({"role": "user"}) is False


def test_resolve_models_gating():
    # normal user can never opt into other models — always the fast default
    assert _resolve_models({"role": "user", "session_kind": "user"}, '["idm_vton"]') == ["catvton"]
    # dev sessions may request a subset; unknown ids are dropped
    dev = {"role": "test", "session_kind": "user"}
    assert _resolve_models(dev, None) == ["catvton"]
    chosen = _resolve_models(dev, '["catvton","flux_kontext"]')
    assert "catvton" in chosen and set(chosen) <= set(tryon.available_models())
    # garbage JSON falls back to catvton
    assert _resolve_models(dev, "not-json") == ["catvton"]
    # a dev request that names only an unavailable model still yields catvton
    assert "catvton" in _resolve_models(dev, '["does_not_exist"]')


def test_run_tryon_model_unknown_raises():
    import asyncio

    from app.tryon import ComfyUnavailable

    # A backend with no workflow/renderer must fail gracefully (per-model error),
    # never 500 the whole request. catvton + idm_vton have real renderers
    # (need GPU + weights), so we assert the graceful failure of an unwired one.
    async def _call():
        class G:  # noqa: D106
            user_id = 1
            image_path = None
        await tryon.run_tryon_model("flux_kontext", b"person", G(), 1)

    try:
        asyncio.run(_call())
    except ComfyUnavailable as ex:
        assert "flux_kontext" in str(ex)
        return
    raise AssertionError("expected ComfyUnavailable for unconfigured model")


def test_to_pants_mask_makes_two_legs():
    """A dress-shaped 'lower' AutoMasker mask (wide A-line blob) must become a
    PANTS shape: a waistband + two separate leg columns, and a waist fraction.
    This is what stops IDM painting a pair of jeans as a denim dress."""
    from io import BytesIO

    from PIL import Image

    m = Image.new("L", (100, 200), 0)
    for y in range(40, 200):  # A-line: narrow at top, wide at bottom
        half = 10 + int((y - 40) / 160 * 35)
        for x in range(50 - half, 50 + half + 1):
            m.putpixel((x, y), 255)
    buf = BytesIO()
    m.save(buf, "PNG")

    pants, waist = tryon._to_pants_mask(buf.getvalue())
    assert 0 < waist < 1
    p = Image.open(BytesIO(pants)).convert("L")
    # near the bottom the white must be TWO separated runs (two legs),
    # never one wide blob (dress)
    xs = [x for x in range(p.width) if p.getpixel((x, p.height - 10)) > 128]
    runs = []
    prev = None
    for x in xs:
        if prev is None or x - prev > 1:
            runs.append([x, x])
        else:
            runs[-1][1] = x
        prev = x
    assert len(runs) >= 2, runs
    # the waistband row (just above the legs) is one solid band
    wy = int(waist * p.height)
    band = [x for x in range(p.width) if p.getpixel((x, wy + 2)) > 128]
    assert len(band) > 10, len(band)


def test_composite_at_waist_splits_top_and_bottom():
    """The top render wins above the waist, the bottom render below it
    (feathered, so dominance not exact equality)."""
    from io import BytesIO

    from PIL import Image

    def _png(color):
        im = Image.new("RGB", (64, 64), color)
        buf = BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue()

    top = _png((255, 0, 0))     # red = the top
    bottom = _png((0, 0, 255))  # blue = the jeans
    out = Image.open(BytesIO(tryon._composite_at_waist(top, bottom, 0.5)))
    r, g, b = out.getpixel((32, 16))
    assert r > b, (r, g, b)  # above waist = top
    r, g, b = out.getpixel((32, 48))
    assert b > r, (r, g, b)  # below waist = jeans


def test_to_top_mask_trims_below_waist():
    """A whole-dress 'upper' mask must be trimmed at the waist so a top
    renders as a top (hem at the waist), not a dress-length garment."""
    from io import BytesIO

    from PIL import Image

    m = Image.new("L", (100, 200), 0)
    for y in range(20, 190):  # whole-dress blob (upper mask on a one-piece)
        for x in range(25, 75):
            m.putpixel((x, y), 255)
    buf = BytesIO()
    m.save(buf, "PNG")

    trimmed = tryon._to_top_mask(buf.getvalue(), 0.5)
    t = Image.open(BytesIO(trimmed)).convert("L")
    assert t.getpixel((50, 60)) > 128   # above the waist: still covered
    assert t.getpixel((50, 150)) == 0   # below the waist: trimmed
    assert t.getpixel((50, 199)) == 0


def test_waist_fraction_mid_band():
    """The waist is the narrowest row in the mid-band of the body blob (not the
    top edge of the silhouette)."""
    from io import BytesIO

    from PIL import Image

    m = Image.new("L", (100, 200), 0)
    # A-line blob: wide at top (chest) + bottom (skirt), narrow at waist (row 90)
    for y in range(20, 200):
        half = 20 + int((y - 20) / 180 * 20)
        if 80 <= y <= 100:
            half = 8  # the waist pinch
        for x in range(50 - half, 50 + half + 1):
            m.putpixel((x, y), 255)
    buf = BytesIO()
    m.save(buf, "PNG")
    wf = tryon._waist_fraction(buf.getvalue())
    assert 0.3 < wf < 0.7, wf


def test_photo_style_from_mask():
    """A dress source gives a high-starting 'lower' mask (dress); a separates
    source gives a low-starting mask (separates) — the logic that auto-picks
    the best person photo for a top+bottom look."""
    from io import BytesIO

    from PIL import Image

    def mask_start(start_y):
        m = Image.new("L", (100, 200), 0)
        for y in range(start_y, 200):
            for x in range(30, 70):
                m.putpixel((x, y), 255)
        buf = BytesIO()
        m.save(buf, "PNG")
        return buf.getvalue()

    # dress: the 'lower' mask covers the torso too (starts high, ~20%)
    assert tryon.photo_style_from_mask(mask_start(40)) == "dress"
    # a maxi dress / long skirt also starts ~40% -> still a dress
    assert tryon.photo_style_from_mask(mask_start(80)) == "dress"   # 80/200=0.40
    # separates: the mask is confined to the lower body (starts ~50-60%)
    assert tryon.photo_style_from_mask(mask_start(100)) == "separates"  # 100/200=0.50
    assert tryon.photo_style_from_mask(mask_start(120)) == "separates"  # 120/200=0.60


if __name__ == "__main__":
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    sys.exit(1 if failed else 0)
