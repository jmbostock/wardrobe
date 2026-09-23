"""Try-on renderer tests — prompt construction, pass scheduling, model dispatch.

Runnable without pytest:
    python services/webapp/tests/test_tryon_models.py

These are the tests that guard behaviours which were expensive to discover.
The CatVTON/IDM mask tests that used to live here were removed with that stack
(2026-09-23); everything below covers the Qwen-Image-2.1 renderer that replaced
it.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="cluelesscloset-tryon-models-test-"))
os.environ.setdefault("TRYON_MODELS", "qwen_edit")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import tryon  # noqa: E402


class G:
    """Minimal stand-in for a wardrobe Garment (only .category/.name are read
    by the prompt + pass-scheduling code under test)."""

    def __init__(self, category: str, name: str = "item"):
        self.category = category
        self.name = name


# --------------------------------------------------------------------------- #
# model dispatch                                                              #
# --------------------------------------------------------------------------- #

def test_run_tryon_model_unknown_raises():
    """A backend with no renderer must fail gracefully (per-model error), never
    500 the whole request."""
    import asyncio

    async def _call():
        await tryon.run_tryon_model("flux_kontext", b"person", G("top"), 1)

    try:
        asyncio.run(_call())
    except tryon.ComfyUnavailable as ex:
        assert "flux_kontext" in str(ex)
        return
    raise AssertionError("expected ComfyUnavailable for unconfigured model")


def test_qwen_edit_is_the_only_wired_backend():
    """catvton / idm_vton were removed — asking for them must raise, not fall
    back to some other renderer and silently produce a different image."""
    import asyncio

    for dead in ("catvton", "idm_vton"):
        async def _call(_m=dead):
            await tryon.run_tryon_outfit_model(_m, b"person", [G("top")], 1)

        try:
            asyncio.run(_call())
        except tryon.ComfyUnavailable as ex:
            assert dead in str(ex), str(ex)
        else:
            raise AssertionError(f"{dead} should no longer be renderable")


# --------------------------------------------------------------------------- #
# the prompt golden rule                                                      #
# --------------------------------------------------------------------------- #

def test_edit_prompt_never_mentions_garment_names():
    """THE GOLDEN RULE: a prompt that names what a garment LOOKS like overrides
    its reference image.

    Regression guard for the "invented silver trim" blazer (539) and the
    "Navy crewneck" sweater that rendered navy when it was dark grey. Both were
    caused by injecting garment metadata (vision_desc / name) into the prompt.
    If someone reintroduces that, this test fails."""
    g = G("outerwear", "Navy blazer with silver trim")
    prompt = tryon._qwen_edit_prompt([g])
    low = prompt.lower()
    assert "navy" not in low, prompt
    assert "silver" not in low, prompt
    assert "blazer" not in low, prompt
    assert "trim" not in low, prompt
    # ...but the SLOT must still be stated, or the model doesn't know where the
    # reference belongs
    assert "outer layer" in low, prompt


def test_edit_prompt_assigns_reference_roles_in_order():
    prompt = tryon._qwen_edit_prompt([G("bottom"), G("top"), G("outerwear")])
    assert "<image2>" in prompt and "<image3>" in prompt and "<image4>" in prompt
    assert "bottom half" in prompt
    assert " as the top" in prompt
    assert "outer layer" in prompt


def test_edit_prompt_is_positive_only():
    """cfg is 1.0, so a negative prompt is inert — and 'no collage' style
    phrasing in the POSITIVE prompt measurably backfired. Keep it positive."""
    prompt = tryon._qwen_edit_prompt([G("top")]).lower()
    for bad in ("no collage", "do not include", "without any", "avoid "):
        assert bad not in prompt, prompt


def test_refine_prompt_leaves_pose_open():
    """Refine must be able to change a pose. Pinning 'identical pose' would
    actively fight a request like 'turn her to the side'; identity and scene
    still have to be held."""
    prompt = tryon._qwen_refine_prompt("turn her to the side")
    low = prompt.lower()
    assert "turn her to the side" in low
    assert "identical face" in low          # identity held
    assert "background" in low              # scene held
    assert "identical pose" not in low      # pose NOT held
    assert "same pose" not in low


def test_refine_prompt_strips_trailing_period():
    """A trailing '.' from the user would otherwise produce '..' mid-sentence."""
    prompt = tryon._qwen_refine_prompt("make the top long-sleeved.")
    assert "long-sleeved.." not in prompt
    assert "long-sleeved." in prompt


# --------------------------------------------------------------------------- #
# pass scheduling (the 3-garment collapse)                                    #
# --------------------------------------------------------------------------- #

def test_passes_single_pass_for_two_or_fewer():
    assert len(tryon._qwen_passes([G("top")])) == 1
    assert len(tryon._qwen_passes([G("top"), G("bottom")])) == 1


def test_passes_never_mix_lowers_and_uppers():
    """A naive chunk-by-two of [jeans, top, blazer] gives [[jeans, top],
    [blazer]] — leaving the outerwear ALONE in the final pass, where it gets
    dropped (verified: the blazer vanished). Each category group must be chunked
    on its own."""
    jeans, top, blazer = G("bottom"), G("top"), G("outerwear")
    passes = tryon._qwen_passes([jeans, top, blazer])
    assert len(passes) == 2, passes
    for batch in passes:
        kinds = {tryon.CLOTH_TYPE.get(g.category, "upper") for g in batch}
        assert len(kinds) == 1, f"mixed lowers+uppers in one pass: {batch}"
    # the bottom gets its own pass and the two uppers stay together
    assert [g.category for g in passes[0]] == ["bottom"], passes
    assert {g.category for g in passes[1]} == {"top", "outerwear"}, passes


def test_passes_put_lowers_first():
    """Order matters as much as grouping: pass1 [jeans] -> pass2 [top, blazer]
    scored 3/3, while [top, jeans] -> [blazer] dropped the blazer."""
    passes = tryon._qwen_passes([G("top"), G("bottom"), G("outerwear")])
    assert tryon.CLOTH_TYPE.get(passes[0][0].category) == "lower", passes


def test_passes_cap_at_two_references():
    """2 references is the reliable ceiling — 3 in one pass collapsed to 2/3."""
    many = [G("top"), G("top"), G("top"), G("outerwear")]
    for batch in tryon._qwen_passes(many):
        assert len(batch) <= 2, batch
    # every garment still gets rendered exactly once
    flat = [g for batch in tryon._qwen_passes(many) for g in batch]
    assert len(flat) == len(many)


# --------------------------------------------------------------------------- #
# base-photo style classification                                             #
# --------------------------------------------------------------------------- #

def test_person_style_parsing():
    """The vision reply is parsed into the four values the base gate uses. A
    reply we cannot read MUST become 'unknown' — the gate treats 'unknown' as
    'cannot prove a mismatch' and lets the render through, so misreading it as a
    real style would block valid renders."""
    import asyncio

    real = tryon._vision_ask
    try:
        for reply, want in [("DRESS", "dress"), ("shorts\n", "shorts"),
                            ("**PANTS**", "pants"), ("OTHER", "unknown"),
                            ("", "unknown"), ("no idea at all", "unknown")]:
            async def _fake(prompt, data, _r=reply):  # noqa: ANN001
                return _r

            tryon._vision_ask = _fake
            got = asyncio.run(tryon.classify_person_style(b"fake-image-bytes"))
            assert got == want, (reply, got, want)
    finally:
        tryon._vision_ask = real


def test_person_style_survives_vision_failure():
    """Vision down must degrade to 'unknown', never raise — refresh_photo and
    the base gate both call this on a request path."""
    import asyncio

    real = tryon._vision_ask

    async def _boom(prompt, data):  # noqa: ANN001
        raise RuntimeError("vision exploded")

    try:
        tryon._vision_ask = _boom
        assert asyncio.run(tryon.classify_person_style(b"x")) == "unknown"
    finally:
        tryon._vision_ask = real


# --------------------------------------------------------------------------- #
# garment name fallback                                                       #
# --------------------------------------------------------------------------- #

def test_is_shorts_detects_by_name():
    """Only a last-resort fallback for when vision is unavailable — but it must
    still not fire on a 'shortsleeve top'."""
    g = G("bottom", "Black shorts")
    assert tryon._is_shorts(g) is True
    g.name = "Cargo shorts"
    assert tryon._is_shorts(g) is True
    g.name = "Shorts"  # bare name
    assert tryon._is_shorts(g) is True
    g.name = "Blue jeans"
    assert tryon._is_shorts(g) is False
    g.name = "Athletic pants"
    assert tryon._is_shorts(g) is False
    g.category = "top"
    g.name = "shortsleeve top"  # 'short' substring, but not a bottom
    assert tryon._is_shorts(g) is False


# --------------------------------------------------------------------------- #
# refine entry point                                                          #
# --------------------------------------------------------------------------- #

def test_refine_rejects_empty_instruction():
    """An empty instruction would send a no-op edit to the GPU — reject it here
    rather than burning ~100s of render time."""
    import asyncio

    for blank in ("", "   ", "\n"):
        try:
            asyncio.run(tryon.refine_render(b"base", blank))
        except tryon.ComfyUnavailable as ex:
            assert "instruction" in str(ex), str(ex)
        else:
            raise AssertionError(f"blank instruction {blank!r} was accepted")


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
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
