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


def test_composite_by_mask_preserves_each_garment_region():
    """The IDM chained-outfit fix: later renders win only inside their own
    AutoMasker region (white), so the first garment (e.g. the top) is never
    dropped. Pure-PIL, no GPU needed."""
    from io import BytesIO

    from PIL import Image

    def _png(color):
        im = Image.new("RGB", (64, 64), color)
        buf = BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue()

    top_render = _png((255, 0, 0))    # red = the top is everywhere here
    bottom_render = _png((0, 0, 255))  # blue = the bottom is everywhere here
    # AutoMasker for the lower garment: white (255) in the LOWER half
    m = Image.new("L", (64, 64), 0)
    for y in range(32, 64):
        for x in range(64):
            m.putpixel((x, y), 255)
    mbuf = BytesIO()
    m.save(mbuf, "PNG")
    mask = mbuf.getvalue()

    out = Image.open(BytesIO(tryon._composite_by_mask([top_render, bottom_render], [mask, mask])))
    assert out.size == (64, 64)
    # The blur feathers the seam, so assert DOMINANCE not exact equality:
    # upper half stays the FIRST render (red wins) — the top is preserved
    r, g, b = out.getpixel((32, 8))
    assert r > b, (r, g, b)
    # lower half becomes the SECOND render (blue wins) — the bottom is applied
    r, g, b = out.getpixel((32, 56))
    assert b > r, (r, g, b)
    # right at the seam the two are blended (neither pure)
    r, g, b = out.getpixel((32, 32))
    assert 0 < r < 255 and 0 < b < 255, (r, g, b)


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
