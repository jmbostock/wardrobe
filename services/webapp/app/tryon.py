"""ComfyUI / CatVTON client for virtual try-on.

ComfyUI is internal-only (comfyui:8188). Flow:
  1. upload person + garment images to ComfyUI /upload/image
  2. load workflows/catvton.json, wire the uploaded image names + cloth_type
  3. submit to /prompt, poll /history/{id}
  4. return the rendered image bytes

The CatVTON node (release `ComfyUI-CatVTON.zip`) exposes:
  LoadAutoMasker / AutoMasker (cloth_type: upper|lower|overall)
  LoadCatVTONPipeline / CatVTON (try-on)
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import random
import re
import time
from pathlib import Path

import httpx
from PIL import Image, ImageChops, ImageFilter, ImageOps

from . import db
from .config import settings
from .wardrobe import Garment

WORKFLOW_PATH = Path(__file__).parent / "workflows" / "catvton.json"

# node ids in workflows/catvton.json
NODE_IDS = {
    "person_image": "10",
    "garment_image": "11",
    "masker_pipe": "12",
    "automasker": "13",
    "tryon_pipe": "17",
    "catvton": "16",
    "output": "18",
}

# CatVTON's node center-crops every person image to this canvas (768x1024, 3:4).
# We letterbox to the same canvas first so that crop is a no-op and the head is
# never cut off (non-3:4 / EXIF-rotated photos were losing their tops).
CATVTON_W = 768
CATVTON_H = 1024

# Fallback face-band top (fraction of frame height) used by _restore_face when
# skin-tone face detection (_detect_face_top) finds no face. This mirrors the
# original fixed "top 14%" band so a render still hard-restores the face region
# instead of crashing on a missing face.
FACE_SAFE_TOP = 0.14

# garment category -> CatVTON cloth_type
CLOTH_TYPE = {
    "top": "upper",
    "outerwear": "upper",
    "dress": "overall",
    "swimsuit": "overall",
    "bottom": "lower",
    "bra": "upper",
}


class ComfyUnavailable(Exception):
    """ComfyUI is missing, errored, or timed out — surfaced as HTTP 503."""





async def run_tryon_model(
    model: str, person_bytes: bytes, garment: Garment, user_id: int
) -> bytes:
    """Render one garment with a named model backend."""
    if model == "catvton":
        return await run_tryon(person_bytes, garment, user_id)
    if model == "idm_vton":
        return await _run_idm_vton(person_bytes, garment, user_id)
    raise ComfyUnavailable(
        f"model '{model}' has no renderer wired in tryon.py"
    )


async def run_tryon_outfit_model(
    model: str, person_bytes: bytes, garments: list[Garment], user_id: int
) -> bytes:
    """Render a WHOLE outfit (multiple garments) with a named model backend.

    catvton  — chains render-onto-render (CatVTON is a true inpainter, so the
               previously-applied garment survives — this is the proven path).
    idm_vton — CHAINS render-onto-render but gives bottoms a PANTS-shaped mask
               (AutoMasker 'lower' reshaped into two legs) + a clear "pants"
               description, so the top from the input survives AND the jeans
               come out as proper pants. (Plain chaining drops the top; a
               dress-length mask makes a denim dress — both verified.)
    """
    if model == "catvton":
        out = person_bytes
        for g in garments:
            out = await run_tryon(out, g, user_id)
        return out
    if model == "idm_vton":
        return await _run_idm_vton_outfit(person_bytes, garments, user_id)
    raise ComfyUnavailable(
        f"model '{model}' has a workflow but no outfit renderer wired in tryon.py"
    )


# --- IDM-VTON (SDXL) backend ---
IDM_WORKFLOW_PATH = Path(__file__).parent / "workflows" / "idm_vton.json"
IDM_MASK_WORKFLOW_PATH = Path(__file__).parent / "workflows" / "idm_vton_mask.json"
IDM_NODE_IDS = {
    "person_image": "10",
    "garment_image": "11",
    "mask_image": "13",
    "densepose": "14",
    "pipeline": "15",
    "idm": "16",
    "output": "17",
}
IDM_MASK_NODE_IDS = {
    "person_image": "10",
    "masker_pipe": "12",
    "automasker": "13",
    "output": "18",
}


async def _free_models(client: httpx.AsyncClient) -> None:
    """Ask ComfyUI to unload all loaded models + clear the torch cache.

    The IDM-VTON pipeline is ~13.7GB on the 5060 Ti and stays resident once
    loaded (ComfyUI caches the PipelineLoader). Without this, a CHAINED outfit
    (top → bottom) OOMs: the 2nd garment's mask pass (AutoMasker/DensePose)
    runs while the 1st garment's pipeline is still loaded. We free at the START
    (clear any leftover CatVTON/IDM models before the mask pass) and at the END
    (unload the pipeline so the next chained render / next request fits).
    Failures are ignored — a stale load just risks a later OOM, never a 500."""
    try:
        await client.post(
            "/free",
            json={"unload_models": True, "free_memory": True},
            timeout=10,
        )
    except Exception:  # noqa: BLE001
        pass


# IDM-VTON interprets the garment partly from the free-text description — the
# default ("The garment shown...") leaves it to guess. Give category-aware,
# plain instructions so a "bottom" is never read as a dress/skirt (hard
# requirement: top vs bottom must be respected — a pair of jeans must stay
# pants).
_GARMENT_DESCRIPTIONS = {
    "upper": "a top worn on the upper body, covering the torso from the shoulders to the waist, with sleeves for the arms",
    "lower": "a pair of pants worn on the lower body — each leg covers one leg from the hips down to the ankles, with a waistband at the waist. It is NOT a dress and NOT a skirt. The fabric/color is whatever the reference garment shows — do not default it to denim or blue.",
    "shorts": "a pair of shorts worn on the lower body, ending above the knee with the bare legs visible below",
    "overall": "a one-piece garment (dress or overall) covering the torso and the legs",
}
_DEFAULT_GARMENT_DESC = "The garment shown in the reference image."


async def _idm_cleanup_garment(
    client: httpx.AsyncClient,
    person_name: str,
    garment: Garment,
    user_id: int,
    seed: int,
    area_mask: bytes | None = None,
    resolution: tuple[int, int] | None = None,
) -> bytes:
    """Per-piece try-on of ONE garment — CatVTON defines the area, IDM cleans
    up the texture (user-directed architecture 2026-08-25).

      pass 1 — CatVTON (a reliable inpainter) places the garment correctly and
               its AutoMasker mask IS the area. We capture both from ONE prompt
               (an extra SaveImage on AutoMasker). `area_mask` can instead be
               supplied by the caller (outfit phase 1) so the area is reused.
      pass 2 — IDM-VTON re-renders ONLY that area (the same mask) on the
               CatVTON result, so it sees the correct boundaries + the rest of
               the outfit as context, and re-textures the piece from the
               flat-lay reference. The mask is the only geometry given — no
               injected garment descriptions (LET THE MODEL DECIDE on texture).

    Returns the composite (CatVTON person with this piece's IDM texture)."""
    garment_bytes = _load_garment_image(garment, user_id)
    cloth_type = CLOTH_TYPE.get(garment.category, "upper")
    # CatVTON uses the RAW flat-lay (geometry/placement — background doesn't
    # matter). IDM gets the BACKGROUND-REMOVED garment so it textures the actual
    # fabric, not the backdrop it was shot on (beige background → beige shorts;
    # a white drawstring gets over-painted as a thick band).
    cat_garment_name = await _upload(client, "garment.png", garment_bytes)
    idm_garment_name = await _upload(
        client, "garment_clean.png", _idm_garment_bytes(garment, user_id)
    )

    if area_mask is None:
        # --- pass 1: CatVTON defines the area ---
        # For SHORTS the mask must be capped at the thigh BEFORE CatVTON
        # renders: the raw AutoMasker 'lower' mask runs waist -> ankles on bare
        # legs, so CatVTON paints the shorts over the whole leg = long pants
        # (the O-X9992S failure). Cap it first, feed CatVTON the capped mask.
        if cloth_type == "lower" and _is_shorts(garment):
            raw = await _automasker_mask(client, person_name, cloth_type)
            area, _waist = _to_shorts_mask(raw)
            mask_name = await _upload(client, "mask.png", area)
            cat_render = await _catvton_with_mask(
                client, person_name, cat_garment_name, mask_name, seed
            )
        else:
            # non-shorts: one CatVTON prompt gives render + its AutoMasker mask
            cat = json.loads(WORKFLOW_PATH.read_text())
            cn = NODE_IDS  # catvton node ids
            cat[cn["person_image"]]["inputs"]["image"] = person_name
            cat[cn["garment_image"]]["inputs"]["image"] = cat_garment_name
            cat[cn["automasker"]]["inputs"]["cloth_type"] = cloth_type
            cat[cn["catvton"]]["inputs"]["seed"] = seed
            # extra SaveImage on AutoMasker so we get the garment's area back
            cat["19"] = {
                "class_type": "SaveImage",
                "inputs": {"images": [cn["automasker"], 0], "filename_prefix": "idm_area_mask"},
            }
            entry = await _poll(client, await _submit(client, cat))
            outs = await _fetch_outputs(client, entry, {cn["output"], "19"})
            cat_render = outs[cn["output"]]
            area = outs.get("19", cat_render)
        human_name = await _upload(client, "catvton.png", cat_render)
    else:
        # Caller already has the CatVTON render + area (outfit phase 1): the
        # passed person_name IS the CatVTON composite — no CatVTON pass here.
        human_name = person_name
        area = area_mask

    # --- pass 2: IDM cleans up the texture within the defined area ---
    await _free_models(client)  # drop CatVTON (~6GB) before the ~13.7GB pipeline
    mask_name = await _upload(client, "mask.png", area)
    idm = json.loads(IDM_WORKFLOW_PATH.read_text())
    in_ = IDM_NODE_IDS
    if resolution:  # outfit chains run each piece smaller to fit 16GB
        idm[in_["idm"]]["inputs"]["width"] = resolution[0]
        idm[in_["idm"]]["inputs"]["height"] = resolution[1]
    idm[in_["person_image"]]["inputs"]["image"] = human_name  # CatVTON result = context
    idm[in_["garment_image"]]["inputs"]["image"] = idm_garment_name  # background-removed garment
    idm[in_["mask_image"]]["inputs"]["image"] = mask_name  # area = CatVTON's defined piece
    # The IDM prompt is TIED TO THE ACTUAL GARMENT — from the STORED vision
    # description (computed at upload / nightly, no live vision here: IDM
    # renders need vision STOPPED for VRAM, so the render path must never call
    # the vision model). Falls back to the category text only when uncached.
    _gdesc = _GARMENT_DESCRIPTIONS.get(cloth_type, _DEFAULT_GARMENT_DESC)
    if cloth_type == "lower" and _is_shorts(garment):
        _gdesc = _GARMENT_DESCRIPTIONS.get("shorts", _gdesc)
    _stored_type, _stored_desc = get_garment_vision(garment.id)
    if _stored_desc:
        _gdesc = _stored_desc
    idm[in_["idm"]]["inputs"]["garment_description"] = _gdesc
    idm[in_["idm"]]["inputs"]["seed"] = seed
    entry = await _poll(client, await _submit(client, idm))
    return await _fetch_output(client, entry)


async def _automasker_mask(
    client: httpx.AsyncClient, person_name: str, cloth_type: str
) -> bytes:
    """Run CatVTON's LoadAutoMasker/AutoMasker (idm_vton_mask.json) → the raw
    garment mask. Used when the mask must be edited before rendering (shorts:
    cap at the thigh, else CatVTON paints long pants on the raw waist->ankle
    'lower' mask)."""
    wf = json.loads(IDM_MASK_WORKFLOW_PATH.read_text())
    mn = IDM_MASK_NODE_IDS
    wf[mn["person_image"]]["inputs"]["image"] = person_name
    wf[mn["automasker"]]["inputs"]["cloth_type"] = cloth_type
    entry = await _poll(client, await _submit(client, wf))
    return await _fetch_output(client, entry)


def _bare_leg_fraction(image_bytes: bytes) -> float:
    """Fraction of skin-tone pixels in the lower-leg band (mid-thigh to just
    above the shoes). A SHORTS/bare-leg base scores high, a PANTS base low.
    AutoMasker's 'lower' mask runs waist→ankles on every separates base, so the
    mask geometry can't tell shorts from pants — the skin in the lower legs
    can. (Same HSV skin test as imageqa.)"""
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes))).convert("RGB")
    except Exception:  # noqa: BLE001
        return 0.0
    w, h = img.size
    crop = img.crop((0, int(h * 0.58), w, int(h * 0.92)))
    crop.thumbnail((160, 160))
    hsv = crop.convert("HSV")
    skin = total = 0
    for hh, s, v in hsv.getdata():
        total += 1
        if (hh <= 25 or hh >= 335) and 40 <= s <= 175 and v >= 90:
            skin += 1
    return skin / total if total else 0.0


async def _classify_base_style(client: httpx.AsyncClient, person_name: str, person_bytes: bytes) -> str:
    """Classify a person photo's lower-body clothing (deterministic, no vision
    model):
      'dress'  — AutoMasker 'lower' mask starts high (covers the torso) → one-piece
      'shorts' — separates base with bare legs (lower-leg skin fraction high)
      'pants'  — separates base with covered legs (low skin)
      'unknown' — no lower mask / ComfyUI hiccup
    This guarantees a SHORTS garment gets a shorts/bare-leg base and a PANTS
    garment gets a separates base — never a dress, never pants-for-shorts."""
    try:
        raw = await _automasker_mask(client, person_name, "lower")
    except Exception:  # noqa: BLE001
        return "unknown"
    m = Image.open(io.BytesIO(raw)).convert("L")
    w, h = m.size
    rows = []
    for y in range(h):
        xs = [x for x in range(w) if m.getpixel((x, y)) > 128]
        if xs:
            rows.append((y, min(xs), max(xs)))
    if not rows:
        return "unknown"
    y_top = rows[0][0]
    if y_top / h < 0.45:
        return "dress"  # mask starts high → one-piece
    # separates: bare legs below the hem = shorts base; covered = pants base
    return "shorts" if _bare_leg_fraction(person_bytes) > 0.15 else "pants"


async def classify_person_style(person_bytes: bytes) -> str:
    """Convenience wrapper: classify a person photo (dress/shorts/pants/unknown)
    via AutoMasker + bare-leg skin check, opening its own ComfyUI client. Used
    by the base picker and the render-time base gate."""
    async with httpx.AsyncClient(timeout=30) as client:
        await _free_models(client)
        pn = await _upload(client, "person.png", _prep_person(person_bytes))
        return await _classify_base_style(client, pn, person_bytes)


async def _catvton_with_mask(
    client: httpx.AsyncClient,
    person_name: str,
    garment_name: str,
    mask_name: str,
    seed: int,
) -> bytes:
    """CatVTON render with a PRE-MADE mask (LoadImage) instead of the built-in
    AutoMasker — so CatVTON paints within the exact area we give it (e.g. a
    thigh-capped shorts mask)."""
    cat = json.loads(WORKFLOW_PATH.read_text())
    cn = NODE_IDS
    cat[cn["person_image"]]["inputs"]["image"] = person_name
    cat[cn["garment_image"]]["inputs"]["image"] = garment_name
    cat[cn["catvton"]]["inputs"]["seed"] = seed
    cat.pop(cn["automasker"], None)   # "13" — replace built-in mask with ours
    cat.pop(cn["masker_pipe"], None)  # "12"
    cat["30"] = {"class_type": "LoadImage", "inputs": {"image": mask_name}}
    cat[cn["catvton"]]["inputs"]["mask_image"] = ["30", 0]
    entry = await _poll(client, await _submit(client, cat))
    return await _fetch_output(client, entry)


async def _run_idm_vton(
    person_bytes: bytes, garment: Garment, user_id: int, seed: int | None = None
) -> bytes:
    """Per-piece IDM try-on of ONE garment: CatVTON defines the area, IDM
    cleans up the texture (see _idm_cleanup_garment)."""
    if not IDM_WORKFLOW_PATH.exists() or not WORKFLOW_PATH.exists():
        raise ComfyUnavailable("workflows/idm_vton.json / catvton.json missing — see workflows/README.md")
    person_bytes = _prep_person(person_bytes)
    if seed is None:
        seed = settings.tryon_seed if settings.tryon_seed is not None else random.randint(0, 2**31)
    async with httpx.AsyncClient(timeout=30) as client:
        await _free_models(client)
        person_name = await _upload(client, "person.png", person_bytes)
        render = await _idm_cleanup_garment(client, person_name, garment, user_id, seed)
        # hard-restore the original face (user's hard rule: never touch the face)
        render = _restore_face(render, person_bytes)
        await _free_models(client)
        return render


# --- best-source-photo selection -------------------------------------------
# Photo selection is handled by the EXISTING photopick module (vision outfit-
# match with a pure-PIL fallback), wired in tryon_routes._pick_person_photo.
# `photo_style_from_mask` is a pure-PIL helper (dress vs separates from the
# AutoMasker 'lower' mask start height) kept for tests + debugging — a dress
# base makes bottoms render as a SKIRT, a separates base as PANTS.
def photo_style_from_mask(mask_bytes: bytes) -> str:
    """'dress' | 'separates' — from where the AutoMasker 'lower' mask starts
    vertically. On a one-piece dress the lower mask starts high (it covers the
    torso too); on separates it starts low (confined to the lower body).
    Threshold 0.45: a maxi dress / long skirt starts ~0.40, jeans+shirt ~0.50."""
    m = Image.open(io.BytesIO(mask_bytes)).convert("L")
    w, h = m.size
    need = max(3, w // 50)
    for y in range(0, h, 8):
        white = sum(1 for x in range(0, w, 8) if m.getpixel((x, y)) > 128)
        if white >= need:
            return "dress" if (y / h) < 0.45 else "separates"
    return "separates"


async def _run_idm_vton_outfit(
    person_bytes: bytes, garments: list[Garment], user_id: int, seed: int | None = None
) -> bytes:
    """IDM-VTON complete outfit — CatVTON owns geometry, IDM only supplies texture.

    THE RULE (user, 2026-08-31): CatVTON is the one that gets placement right.
    Whatever CatVTON finds (mask + boundaries), IDM follows — IDM is only there
    for texture/color. So:

      phase 1 — CatVTON builds the whole-outfit composite (straight seams), and
                we capture ITS mask for every garment (shorts get capped at the
                thigh BEFORE CatVTON renders, so CatVTON itself paints shorts,
                not long pants).
      phase 2 — IDM re-textures ONE garment at a time ON the CatVTON composite
                using CatVTON's exact mask for that piece. IDM never sees a bare
                person and never decides where a garment goes — it only replaces
                the fabric/color inside CatVTON's area. Each pass re-textures
                only its own masked region, so no colors bleed between pieces.

    Returns the final composite (CatVTON geometry + IDM texture on every piece)."""
    if not garments:
        raise ComfyUnavailable("no garments to render")
    if len(garments) == 1:
        return await _run_idm_vton(person_bytes, garments[0], user_id, seed)
    if not IDM_WORKFLOW_PATH.exists() or not WORKFLOW_PATH.exists():
        raise ComfyUnavailable("workflows/idm_vton.json / catvton.json missing — see workflows/README.md")
    if seed is None:
        seed = settings.tryon_seed if settings.tryon_seed is not None else random.randint(0, 2**31)
    person_bytes = _prep_person(person_bytes)

    async with httpx.AsyncClient(timeout=30) as client:
        await _free_models(client)
        base_name = await _upload(client, "person.png", person_bytes)

        # 0) Capture EVERY garment's mask ONCE from the ORIGINAL base, BEFORE
        # any garment is painted — so the output is INDEPENDENT of garment
        # order. AutoMasker's masks are order-dependent if computed mid-pipeline:
        # the 'lower' mask on a composite (tee already painted) starts at the
        # TORSO (0.33h) → shorts cover the shirt ("shorts as a shirt"); the
        # 'upper' mask on a composite (shorts already painted) extends into the
        # shorts → the tee bleeds down (the O-24WYUD failure vs O-3BXB5D which
        # ran tee-first). Capturing every mask from the bare base once makes
        # geometry stable end to end regardless of the garment order the API
        # received.
        masks: dict[int, bytes] = {}  # garment_id -> mask (from the bare base)
        lower_waist = 0.55  # true waist (for trimming upper masks), from the lower mask
        # lower mask FIRST so lower_waist is known before any upper is trimmed
        # (garment order must not change the geometry).
        lower_garment = next((g for g in garments
                              if CLOTH_TYPE.get(g.category, "upper") == "lower"), None)
        if lower_garment is not None:
            raw_lower = await _automasker_mask(client, base_name, "lower")
            if _is_shorts(lower_garment):
                lower_area, lower_waist = _to_shorts_mask(raw_lower)
            else:
                lower_area, lower_waist = _to_pants_mask(raw_lower)
            masks[lower_garment.id] = lower_area
            await _free_models(client)
        for g in garments:
            if g.id in masks:
                continue  # already captured (the lower)
            cloth = CLOTH_TYPE.get(g.category, "upper")
            raw = await _automasker_mask(client, base_name, cloth)
            # outerwear (jacket/bomber) renders too small in the fitted torso
            # mask — dilate so it covers the shoulders/arms (user: bomber too
            # small, O-29TGRJ fits better).
            if g.category == "outerwear":
                raw = _expand_mask(raw, pct=0.10)
            # upper/overall: trim to end at the lower garment's waist so a
            # top never bleeds into the shorts/pants below it.
            if lower_garment is not None:
                area = _to_top_mask(raw, lower_waist)
            else:
                area = raw
            masks[g.id] = area
            await _free_models(client)

        # 1) CatVTON builds the whole-outfit composite, using EVERY garment's
        #    pre-captured bare-base mask (via _catvton_with_mask) — never its
        #    own AutoMasker on the running composite. That removes the last
        #    order-dependence: CatVTON's internal AutoMasker on a composite
        #    (shorts already painted) makes the tee extend down into the shorts
        #    (the O-24WYUD bleed vs the O-3BXB5D tee-first winner). One mask per
        #    garment, from the bare base, used by both CatVTON and IDM.
        # Canonical layering order: CatVTON chaining is inherently sequential
        # (each render conditions on the previous composite), so the ORDER must
        # not depend on the API's garment order. Tops/uppers always go first,
        # bottoms last — the verified-good tee-first layering (O-HCMMAA).
        # Reordering internally makes input order irrelevant: [626,509] ==
        # [509,626] == the winner. (O-9XBJMA proved shorts-first still degraded:
        # tee/shorts overlap + lost drawstring because the tee was rendered
        # onto a composite that already had the shorts painted.)
        canon = ([g for g in garments if CLOTH_TYPE.get(g.category, "upper") != "lower"]
                 + [g for g in garments if CLOTH_TYPE.get(g.category, "upper") == "lower"])
        cat_composite = person_bytes
        for g in canon:
            pn = await _upload(client, "person.png", cat_composite)
            gb = _load_garment_image(g, user_id)
            gn = await _upload(client, "garment.png", gb)
            cap = masks.get(g.id)
            if cap is None:
                # no pre-captured mask (shouldn't happen) — fall back to CatVTON's own
                cloth = CLOTH_TYPE.get(g.category, "upper")
                cat = json.loads(WORKFLOW_PATH.read_text())
                cn = NODE_IDS
                cat[cn["person_image"]]["inputs"]["image"] = pn
                cat[cn["garment_image"]]["inputs"]["image"] = gn
                cat[cn["automasker"]]["inputs"]["cloth_type"] = cloth
                cat[cn["catvton"]]["inputs"]["seed"] = seed
                entry = await _poll(client, await _submit(client, cat))
                cat_composite = await _fetch_output(client, entry)
                await _free_models(client)
                # CatVTON re-generates the frame too — never let it touch the face
                continue
            mask_name = await _upload(client, "mask.png", cap)
            cat_composite = await _catvton_with_mask(
                client, pn, gn, mask_name, seed
            )
            await _free_models(client)
            # CatVTON re-generates the frame too — never let it touch the face

        # 2) IDM re-textures each garment ON the CatVTON composite, using
        #    the SAME bare-base mask. IDM only replaces texture — geometry stays
        #    CatVTON's. Each pass touches only its own masked area (no bleed).
        current = cat_composite
        for g in canon:
            area = masks.get(g.id)
            if area is None:
                continue
            current_pre = current  # CatVTON composite before this IDM pass
            # INDEPENDENT context — the ORIGINAL bare base, NOT the running
            # composite. IDM-VTON re-generates the whole frame, so if it can SEE
            # another garment in its context it will bleed that garment's color
            # into its own region (the navy bomber colored the green joggers in
            # O-ZRQHH7 AND O-YHVZQ4). Running every pass on the bare base means
            # IDM sees ONLY its own garment + its own mask (user's hard rule).
            # The final color is corrected deterministically AFTER the render
            # (_match_garment_color) — never by trusting IDM's scene harmonization.
            base_name = await _upload(client, "person.png", person_bytes)
            current = await _idm_cleanup_garment(
                client, base_name, g, user_id, seed,
                area_mask=area,
            )
            # IDM stays in its lane: only its mask region shows on the final
            current = _composite_masked(current, current_pre, area)
            await _free_models(client)
        # FINAL clothes-only composite (user's hard rule: ONLY clothes change).
        # CatVTON re-generates the whole frame, so its composite has a drifted
        # background/walls. The union of every garment mask covers the clothes;
        # everywhere OUTSIDE it, the ORIGINAL base photo is pasted back — the
        # background, walls, face, neck and exposed skin become pixel-identical
        # to the source. Nothing but clothes can ever change.
        union = _merge_masks(list(masks.values()))
        current = _composite_masked(current, person_bytes, union)
        current = _restore_face(current, person_bytes)
        # TRUE GARMENT COLOR (user's hard rule): shift each garment region to
        # its actual median color, because IDM's scene harmonization drifts it
        # (pants too dark, tops too green). The shift is applied to the mask's
        # INTERIOR only — _match_garment_color erodes the mask so it never
        # paints the AutoMasker overhang, which is what produced the "giant
        # halo" around every garment.
        for g in canon:
            area = masks.get(g.id)
            if area is None:
                continue
            ref = _reference_garment_color(_idm_garment_bytes(g, user_id))
            if ref is not None:
                current = _match_garment_color(current, area, ref, g.category)
        # Release the ~13.7GB IDM pipeline back to ComfyUI. Without this it
        # stays resident and the NEXT render's IDM pass OOMs (the card only has
        # 16GB; a leaked pipeline leaves ~2GB free). Mirrors _run_idm_vton.
        await _free_models(client)
        return current


def _waist_fraction(mask_bytes: bytes) -> float:
    """Anatomical waist row as a fraction of mask height: the narrowest row in
    the 42-62% band of the mask blob's vertical extent (works for a whole-dress
    silhouette too)."""
    m = Image.open(io.BytesIO(mask_bytes)).convert("L")
    w, h = m.size
    rows = []
    for y in range(h):
        xs = [x for x in range(w) if m.getpixel((x, y)) > 128]
        if xs:
            rows.append((y, min(xs), max(xs)))
    if not rows:
        return 0.5
    y_top = rows[0][0]
    y_bot = rows[-1][0]
    lo = y_top + (y_bot - y_top) * 0.42
    hi = y_top + (y_bot - y_top) * 0.62
    mid = [r for r in rows if lo <= r[0] <= hi]
    if not mid:
        return (y_top + y_bot) / 2 / h
    return min(mid, key=lambda r: r[2] - r[1])[0] / h


def _to_top_mask(mask_bytes: bytes, waist: float) -> bytes:
    """Trim an AutoMasker 'upper' mask to end at the waist so a top renders as
    a TOP (hem at the waist), not a dress-length garment. On a one-piece dress
    the 'upper' mask covers the whole dress; without this trim IDM paints the
    top over the whole body (the 'grey long-sleeve as a dress' failure)."""
    m = Image.open(io.BytesIO(mask_bytes)).convert("L")
    w, h = m.size
    wy = int(waist * h)
    for y in range(wy, h):
        for x in range(w):
            m.putpixel((x, y), 0)
    buf = io.BytesIO()
    m.save(buf, "PNG")
    return buf.getvalue()


def _expand_mask(mask_bytes: bytes, pct: float = 0.10) -> bytes:
    """Dilate a garment mask outward so an OUTERWEAR garment (bomber/jacket)
    covers the shoulders/arms instead of just the fitted torso. AutoMasker's
    'upper' mask is sized to the torso, so a jacket rendered inside it looks too
    small (the 'bomber too small' complaint vs O-29TGRJ, which used a looser
    mask path). Expanding ~10% of the mask's width per side makes the jacket
    reach the arms; the bottom is re-clipped to the waist by the caller after."""
    m = Image.open(io.BytesIO(mask_bytes)).convert("L")
    bbox = m.getbbox()
    if bbox is None:
        return mask_bytes
    bw = bbox[2] - bbox[0]
    px = max(3, int(bw * pct))
    m = m.filter(ImageFilter.MaxFilter(px * 2 + 1))
    buf = io.BytesIO()
    m.save(buf, "PNG")
    return buf.getvalue()


def _is_shorts(garment: Garment) -> bool:
    """A bottom garment whose name says shorts.

    AutoMasker's 'lower' mask needs to be capped at the thigh for a shorts
    garment (else IDM stretches it into long pants). Category is 'bottom' for
    both shorts and pants, so the name is the signal — and a "shortsleeve top"
    (category 'top') is correctly excluded."""
    return garment.category == "bottom" and "short" in (garment.name or "").lower()


def _load_garment_bytes(garment: Garment, user_id: int) -> bytes:
    """Load a garment's image bytes (used by the vision classifier)."""
    try:
        return _load_garment_image(garment, user_id)
    except Exception:  # noqa: BLE001
        return b""


def _remove_garment_background(data: bytes, tolerance: int = 28) -> bytes:
    """Background-remove a garment flat-lay (see media.remove_garment_background).

    Kept as a thin re-export for callers that already have the bytes; the
    preferred path is the stored <gid>.clean.png (written at save time, refreshed
    by the nightly backfill) so renders never re-clean."""
    from .media import remove_garment_background as _clean

    return _clean(data, tolerance=tolerance)


def _idm_garment_bytes(garment: Garment, user_id: int) -> bytes:
    """The background-removed garment reference for IDM (garment on blank white,
    no flat-lay backdrop). Prefers the stored <gid>.clean.png (written at save
    time / nightly backfill); falls back to cleaning on the fly only for garments
    that predate cleaning."""
    try:
        d = Path(settings.data_dir) / "wardrobe" / str(garment.user_id)
        clean = d / f"{garment.id}.clean.png"
        if clean.is_file():
            return clean.read_bytes()
    except Exception:  # noqa: BLE001
        pass
    return _remove_garment_background(_load_garment_image(garment, user_id))


_GARMENT_DESCRIBE_PROMPT = (
    "Look at this single clothing item (flat-lay or product photo). Reply with "
    "EXACTLY these two lines and nothing else:\n"
    "TYPE: one of SHORTS, PANTS, DRESS, SKIRT, TOP, OUTERWEAR, OTHER\n"
    "DESC: one short accurate sentence describing the garment the way a stylist "
    "would (colour, cut, length, key features) — enough to re-identify it and "
    "to know what body it covers.\n"
    "Example:\n"
    "TYPE: SHORTS\n"
    "DESC: olive green cargo shorts ending above the knee with side pockets.\n"
)

# TYPE -> base-photo need (see base_type_for)
_GARMENT_TYPE_TO_BASE = {
    "shorts": "shorts",          # bare legs below
    "pants": "pants",            # separates, never a dress
    "skirt": "pants",            # separates (a dress base would merge it)
    "dress": "dress",            # a dress base (pants base makes it render as pants)
    "jumpsuit": "dress",         # one-piece
    "romper": "dress",           # one-piece
}


async def describe_garment(garment_bytes: bytes) -> dict:
    """Accurately DESCRIBE a garment image with vision (never its name):
    returns {'type': 'shorts'|'pants'|'dress'|'skirt'|'top'|'outerwear'|'other',
             'description': '...'}. `description` is a stylist's one-liner used
    to tie the try-on composite to the actual garment. On any failure returns
    {'type': 'other', 'description': ''} so callers degrade safely."""
    if not garment_bytes:
        return {"type": "other", "description": ""}
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(garment_bytes)))
        img.thumbnail((512, 512), Image.LANCZOS)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=85)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        if settings.vision_engine == "llamacpp":
            url = f"{settings.vision_url}/v1/chat/completions"
            content: list[dict] = [{"type": "text", "text": _GARMENT_DESCRIBE_PROMPT},
                                   {"type": "image_url",
                                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]
            payload = {"messages": [{"role": "user", "content": content}],
                       "stream": False, "temperature": 0}
            # long timeout: vision is wake-on-demand, first call after idle cold-starts (~60-90s)
            async with httpx.AsyncClient(timeout=150) as c:
                r = await c.post(url, json=payload)
            if r.status_code != 200:
                return {"type": "other", "description": ""}
            text = (r.json() or {}).get("choices", [{}])[0].get("message", {}).get("content", "")
        else:
            model = os.getenv("OLLAMA_VISION_MODEL", "qwen2.5vl:3b").strip()
            payload = {"model": model, "prompt": _GARMENT_DESCRIBE_PROMPT,
                       "images": [b64], "stream": False, "options": {"temperature": 0}}
            # long timeout: vision is wake-on-demand, first call after idle cold-starts
            async with httpx.AsyncClient(timeout=150) as c:
                r = await c.post(f"{settings.ollama_url}/api/generate", json=payload)
            if r.status_code != 200:
                return {"type": "other", "description": ""}
            text = (r.json() or {}).get("response", "")
    except Exception:  # noqa: BLE001
        return {"type": "other", "description": ""}
    t = (text or "").strip()
    style = "other"
    desc = ""
    tm = re.search(r"TYPE:\s*([A-Z_]+)", t, re.I)
    if tm:
        word = tm.group(1).lower()
        if word in ("shorts", "pants", "dress", "skirt", "top", "outerwear", "other", "jumpsuit", "romper"):
            style = "jumpsuit" if word == "jumpsuit" else word
            style = "romper" if word == "romper" else style
    dm = re.search(r"DESC:\s*(.+)$", t, re.I | re.M)
    if dm:
        desc = dm.group(1).strip().strip('"').strip()
    return {"type": style, "description": desc}


async def base_type_for(garment: Garment, user_id: int) -> str | None:
    """What kind of base photo a garment needs, decided by DESCRIBING the
    garment image with vision (the description is tied to the try-on composite,
    never guessed from the name):
      'shorts' — shorts → bare-leg/shorts base
      'pants'  — pants/skirt → separates base (never a dress)
      'dress'  — dress/jumpsuit/romper → dress base (a pants base renders it as pants)
      None     — top/outerwear/etc → any full-body base
    Falls back to the name keyword ONLY when vision is unavailable (graceful
    degradation — a last resort, not the primary signal)."""
    info = await describe_garment(_load_garment_bytes(garment, user_id))
    base = _GARMENT_TYPE_TO_BASE.get(info["type"])
    if base is not None:
        return base
    # vision down / unparseable → last-resort keyword (bottom garments only)
    if CLOTH_TYPE.get(garment.category, "upper") == "lower":
        return "shorts" if _is_shorts(garment) else "pants"
    return None


# --------------------------------------------------------------------------- #
# cached vision store — base picking is a DB read, not a live vision call     #
# --------------------------------------------------------------------------- #
# At upload (and the nightly rec_weekly batch) we precompute + store:
#   garments.vision_type / vision_desc  — what the garment IS + a stylist line
#   photos.vision_type                 — what the person is wearing in the base
#   photo_embeddings (FashionCLIP)     — for garment↔photo similarity ranking
# The picker below reads those FIRST; live vision is only a fallback for items
# that were never classified (so picking never blocks or flips on a model call).

def get_garment_vision(garment_id: int) -> tuple[str, str]:
    """Stored vision classification for a garment -> (type, description)."""
    conn = db.init()
    with db.lock():
        row = conn.execute(
            "SELECT vision_type, vision_desc FROM garments WHERE id=?", (garment_id,)
        ).fetchone()
    if not row:
        return "", ""
    return row["vision_type"] or "", row["vision_desc"] or ""


def set_garment_vision(garment_id: int, gtype: str, desc: str = "") -> None:
    conn = db.init()
    with db.lock():
        conn.execute(
            "UPDATE garments SET vision_type=?, vision_desc=? WHERE id=?",
            (gtype or "", desc or "", garment_id),
        )
        conn.commit()


def get_photo_vision(photo_id: int) -> str:
    """Stored 'what the person is wearing' classification for a base photo."""
    conn = db.init()
    with db.lock():
        row = conn.execute(
            "SELECT vision_type FROM photos WHERE id=?", (photo_id,)
        ).fetchone()
    return row["vision_type"] or "" if row else ""


def set_photo_vision(photo_id: int, ptype: str) -> None:
    conn = db.init()
    with db.lock():
        conn.execute("UPDATE photos SET vision_type=? WHERE id=?", (ptype or "", photo_id))
        conn.commit()


async def garment_base_type(garment: Garment, user_id: int) -> str | None:
    """What base a garment needs, using the STORED vision classification when
    present (computed at upload / nightly — no live vision). Falls back to a
    live vision describe only for garments that were never classified."""
    gtype, _ = get_garment_vision(garment.id)
    if gtype:
        return _GARMENT_TYPE_TO_BASE.get(gtype)
    return await base_type_for(garment, user_id)


async def photo_style_cached(person_bytes: bytes, photo_id: int | None) -> str:
    """'dress'|'shorts'|'pants'|'unknown' for a base photo, from the STORED
    photo vision when available (no live vision / no ComfyUI mask pass at pick
    time). Falls back to the live classifier only when the photo has no stored
    classification yet."""
    if photo_id is not None:
        stored = get_photo_vision(photo_id)
        if stored:
            return stored
    return await classify_person_style(person_bytes)


def _to_shorts_mask(mask_bytes: bytes) -> tuple[bytes, float]:
    """Cap an AutoMasker 'lower' mask at mid-thigh so a SHORTS garment renders
    as shorts, not long pants. Returns (mask, waist) — waist = the true waist
    row (just below the top of the lower garment on separates), so the outfit
    composite splits top/shorts at the real waist, never the ankles.

    AutoMasker has no shorts/pants distinction — on a bare-leg (shorts-wearing)
    base its 'lower' mask runs waist -> ankles, so IDM stretches the shorts
    fabric down the whole mask. This trims the mask to the true shorts
    footprint: waist (mask top) down to mid-thigh (~38% of the way to the
    ankles, clamped to 0.50-0.72h), with the hem feathered so the legs emerge
    softly instead of a hard cut. This is the opposite of `_to_pants_mask`
    (which EXTENDS a shorts mask down to the ankles to make jeans) — correct
    *input* geometry, not model interference."""
    m = Image.open(io.BytesIO(mask_bytes)).convert("L")
    w, h = m.size
    rows = []
    for y in range(h):
        xs = [x for x in range(w) if m.getpixel((x, y)) > 128]
        if xs:
            rows.append((y, min(xs), max(xs)))
    if not rows:
        return mask_bytes, 0.55
    y_top = rows[0][0]
    y_bot = rows[-1][0]
    # separates-style waist: just below the top of the lower garment (mirror
    # _to_pants_mask) — NOT _waist_fraction (that finds the narrowest row, which
    # on bare legs is the ankles, splitting the composite at 0.8h → the shirt
    # covers the shorts). On a dress base the mask starts high → use the
    # anatomical waist band instead.
    if (y_top / h) > 0.30:
        waist = (y_top + (y_bot - y_top) * 0.04) / h
    else:
        waist = _waist_fraction(mask_bytes)
    # mid-thigh = ~38% of the way from the waist to the ankles (where the mask
    # bottom sits on a bare-leg base); clamp to a sane shorts length.
    ankle = max(y_bot, int(h * 0.95))
    hem = int(y_top + (ankle - y_top) * 0.38)
    hem = min(int(h * 0.72), max(int(h * 0.50), hem))
    for y in range(hem, h):
        for x in range(w):
            m.putpixel((x, y), 0)
    # feathered hem: fade the mask to 0 over the last `feather` rows above the
    # cut so the warp has a soft edge where the bare legs emerge below.
    feather = max(8, int(h * 0.02))
    for y in range(max(0, hem - feather), hem):
        v = int(255 * (hem - y) / feather)
        for x in range(w):
            if m.getpixel((x, y)) > 128:
                m.putpixel((x, y), v)
    buf = io.BytesIO()
    m.save(buf, "PNG")
    return buf.getvalue(), waist


def _largest_component(m: Image.Image) -> Image.Image:
    """Keep only the largest connected white region (the actual body/legs),
    dropping AutoMasker's stray blobs — e.g. a floor shadow next to the legs
    that it hallucinated as a second garment. Bytearray visited-map flood fill;
    no numpy/scipy dependency."""
    w, h = m.size
    px = m.load()
    seen = bytearray(w * h)
    best = None
    best_size = 0
    for y in range(h):
        row = y * w
        for x in range(w):
            i = row + x
            if seen[i] or px[x, y] <= 128:
                continue
            comp = []
            stack = [(x, y)]
            seen[i] = 1
            while stack:
                cx, cy = stack.pop()
                comp.append((cx, cy))
                if cx + 1 < w:
                    j = cy * w + cx + 1
                    if not seen[j] and px[cx + 1, cy] > 128:
                        seen[j] = 1
                        stack.append((cx + 1, cy))
                if cx - 1 >= 0:
                    j = cy * w + cx - 1
                    if not seen[j] and px[cx - 1, cy] > 128:
                        seen[j] = 1
                        stack.append((cx - 1, cy))
                if cy + 1 < h:
                    j = (cy + 1) * w + cx
                    if not seen[j] and px[cx, cy + 1] > 128:
                        seen[j] = 1
                        stack.append((cx, cy + 1))
                if cy - 1 >= 0:
                    j = (cy - 1) * w + cx
                    if not seen[j] and px[cx, cy - 1] > 128:
                        seen[j] = 1
                        stack.append((cx, cy - 1))
            if len(comp) > best_size:
                best_size = len(comp)
                best = comp
    out = Image.new("L", (w, h), 0)
    if best:
        op = out.load()
        for cx, cy in best:
            op[cx, cy] = 255
    return out


def _mask_rows(m: Image.Image) -> list[tuple[int, int, int]]:
    """Rows of the mask that contain white pixels, as (y, min_x, max_x)."""
    w, h = m.size
    rows = []
    for y in range(h):
        xs = [x for x in range(w) if m.getpixel((x, y)) > 128]
        if xs:
            rows.append((y, min(xs), max(xs)))
    return rows


def _pants_mask_rectangular(
    m: Image.Image, w: int, h: int, rows: list[tuple[int, int, int]], waist: float
) -> bytes:
    """Rectangle-synthesis pants mask — ONLY for a dress base, where the
    'lower' mask is a wide A-line dress blob and no leg silhouette exists to
    keep. (The base picker normally avoids dress bases for pants garments, so
    this is a rare fallback.)"""
    wy = int(waist * h)
    row_xs = [x for x in range(w) if m.getpixel((x, wy)) > 128]
    if row_xs:
        cx0, cx1 = min(row_xs), max(row_xs)
    else:
        cx0, cx1 = rows[0][1], rows[0][2]
    cy = (cx0 + cx1) / 2
    leg_w = max(70, min(120, int((cx1 - cx0) * 0.5)))
    y_bot = int(h * 0.90)
    out = Image.new("L", (w, h), 0)
    for y in range(max(0, wy - 6), min(h, wy + 12)):  # waistband
        for x in range(max(0, int(cy - leg_w)), min(w, int(cy + leg_w) + 1)):
            out.putpixel((x, y), 255)
    feather = max(8, int(h * 0.015))
    for y in range(wy + 12, y_bot + 1):  # two legs, gap widening downward
        t = (y - (wy + 12)) / max(1, y_bot - (wy + 12))
        gap = int(26 + 50 * t)
        v = 255
        if y > y_bot - feather:
            v = int(255 * (y_bot - y) / feather)
        for x in range(max(0, int(cy - leg_w)), min(w, int(cy - gap / 2))):
            out.putpixel((x, y), v)
        for x in range(max(0, int(cy + gap / 2)), min(w, int(cy + leg_w) + 1)):
            out.putpixel((x, y), v)
    buf = io.BytesIO()
    out.save(buf, "PNG")
    return buf.getvalue()


def _to_pants_mask(mask_bytes: bytes) -> tuple[bytes, float]:
    """Turn an AutoMasker 'lower' mask into a PANTS mask.

    SEPARATES base: the 'lower' mask already IS the actual legs — keep that
    true silhouette (dropping stray floor/shadow blobs via largest-component)
    instead of synthesising straight-leg rectangles. Straight rectangles were
    wider than the legs + feathered, which is exactly the fuzzy semi-transparent
    border floating next to the legs (the mask/body mismatch the user sees).
    The ankle hem gets only a ~3px fade, not a 15px gradient.

    SHORTS base: the legs stop at mid-thigh — extrude the actual leg shape down
    to the ankle so full-length pants render.

    DRESS base: the 'lower' mask is a wide A-line dress blob with no leg shape
    to trust — fall back to the rectangle synthesis.

    Returns the pants mask + the waist row fraction."""
    m = Image.open(io.BytesIO(mask_bytes)).convert("L")
    w, h = m.size
    rows = _mask_rows(m)
    if not rows:
        return mask_bytes, 0.5
    y_top = rows[0][0]
    y_bot = rows[-1][0]

    if (y_top / h) <= 0.30:
        # dress base — the A-line silhouette can't be trusted for pants geometry
        waist = _waist_fraction(mask_bytes)
        return _pants_mask_rectangular(m, w, h, rows, waist), waist

    # separates: keep the real leg silhouette
    m = _largest_component(m)
    rows = _mask_rows(m)
    if not rows:
        return mask_bytes, 0.5
    # trim thin protrusions at the very top (a stray speck above the hips makes
    # the waist land too high → a skin gap between the top and the pants).
    maxw = max(r[2] - r[1] for r in rows)
    first_wide = next((r for r in rows if (r[2] - r[1]) >= maxw * 0.4), rows[0])
    for y in range(0, first_wide[0]):
        for x in range(w):
            m.putpixel((x, y), 0)
    rows = _mask_rows(m)
    y_top = rows[0][0]
    y_bot = rows[-1][0]
    waist = (y_top + (y_bot - y_top) * 0.04) / h

    # ankle = hard cap just above the shoes. SHORTS base (legs stop above the
    # ankle) → extrude the hem row down; a mask that runs past the ankle (a
    # foot/shadow taper) → trim it. Same ankle cap as before, but the actual
    # leg shape instead of straight columns.
    ankle = int(h * 0.90)
    if y_bot < int(h * 0.85):
        hem = [x for x in range(w) if m.getpixel((x, y_bot)) > 128]
        for y in range(y_bot + 1, ankle + 1):
            for x in hem:
                m.putpixel((x, y), 255)
        y_bot = ankle
    elif y_bot > ankle:
        for y in range(ankle, h):
            for x in range(w):
                m.putpixel((x, y), 0)
        y_bot = ankle

    # hard ankle hem (a couple of rows of fade — not a wide semi-transparent
    # gradient, which is the "fuzzy border" around the legs).
    feather = 3
    for y in range(max(0, y_bot - feather), y_bot + 1):
        v = int(255 * (y_bot - y) / feather)
        for x in range(w):
            if m.getpixel((x, y)) > 128:
                m.putpixel((x, y), v)
    buf = io.BytesIO()
    m.save(buf, "PNG")
    return buf.getvalue(), waist





def _is_skin_px(px) -> bool:
    """HSV skin-tone test (same rule as _bare_leg_fraction)."""
    hh, s, v = px
    return (hh <= 25 or hh >= 335) and 40 <= s <= 175 and v >= 90


def _detect_face_top(base: Image.Image) -> int | None:
    """Locate the top of the face (forehead) as the first dense run of skin
    pixels, on a downscaled copy. The base photos have headroom, so the face
    sits at ~14-21% of frame height — far below the old fixed FACE_SAFE_TOP
    band, which restored only background and left the whole face to IDM's
    drift. Pure-PIL, deterministic. Returns None if no face found (caller falls
    back to the fixed band)."""
    w, h = base.size
    small = base.convert("RGB")
    small.thumbnail((200, 267))
    sw, sh = small.size
    hsv = small.convert("HSV")
    for y in range(int(sh * 0.03), int(sh * 0.35)):
        cnt = 0
        for x in range(0, sw, 2):
            if _is_skin_px(hsv.getpixel((x, y))):
                cnt += 1
        if cnt >= 2:
            following = 0
            for y2 in range(y + 1, min(y + 8, sh)):
                c2 = sum(1 for x in range(0, sw, 2) if _is_skin_px(hsv.getpixel((x, y2))))
                if c2 >= 2:
                    following += 1
            if following >= 3:
                return int(y * h / sh)
    return None


def _restore_face(render_bytes: bytes, base_bytes: bytes) -> bytes:
    """Hard-restore the ORIGINAL face over a render.

    IDM-VTON re-generates the WHOLE frame through the UNet, so even with the
    face zone zeroed in the mask (face_safe) the model can still drift the
    face (eyes, jaw, shape). User's hard rule: clothes-only edits must NEVER
    touch the face. A clothes change never moves the head, so pasting the
    original base photo's face back makes it pixel-identical to the source.

    The band is located by the ACTUAL face position (skin-tone detection) and
    spans only the CENTRAL face column (28-72% width) — NOT the full width — so
    the side-hair that hangs over the shoulders is left to the render instead
    of being dragged down onto the bomber ("hair extended down the shoulder").
    Vertically it runs through the jaw so it never cuts across the mouth. The
    bottom and side edges feather so the seam is invisible."""
    base = Image.open(io.BytesIO(base_bytes)).convert("RGB")
    render = Image.open(io.BytesIO(render_bytes)).convert("RGB")
    if base.size != render.size:
        render = render.resize(base.size, Image.LANCZOS)
    w, h = base.size
    face_top = _detect_face_top(base)
    if face_top is None:
        face_top = int(h * FACE_SAFE_TOP)  # fall back to the fixed band
    band_bottom = min(h, int(face_top + h * 0.11))  # face + chin + jaw (past the mouth)
    feather_y = max(8, int(h * 0.02))
    x0 = int(w * 0.28)
    x1 = int(w * 0.72)
    feather_x = max(8, int(w * 0.02))
    band = base.crop((x0, 0, x1, band_bottom))
    bw = x1 - x0
    bh = band_bottom
    mask = Image.new("L", (bw, bh), 255)
    px = mask.load()
    # bottom feather (into the neck)
    for y in range(max(0, bh - feather_y), bh):
        v = int(255 * (bh - y) / feather_y)
        for x in range(bw):
            px[x, y] = min(px[x, y], v)
    # left/right feathers (so the face edge blends, no hard line)
    for x in range(min(feather_x, bw)):
        v = int(255 * x / feather_x)
        for y in range(bh):
            if px[x, y] > v:
                px[x, y] = v
    for x in range(max(0, bw - feather_x), bw):
        v = int(255 * (bw - x) / feather_x)
        for y in range(bh):
            if px[x, y] > v:
                px[x, y] = v
    mask = mask.filter(ImageFilter.GaussianBlur(max(2, min(feather_y, feather_x) // 5)))
    render.paste(band, (x0, 0), mask)
    buf = io.BytesIO()
    render.save(buf, "PNG")
    return buf.getvalue()


def _reference_garment_color(clean_bytes: bytes) -> tuple[int, int, int] | None:
    """Median color of the ACTUAL garment pixels in a background-removed clean
    image (pixels far from white = the garment). None if nothing usable. This is
    the ONE source of truth for the garment's color — the base photo and scene
    must have zero influence on it (user's hard rule).

    Uses the per-channel MEDIAN, not the mean: the clean image keeps white
    fringes/highlights around the garment that inflate the mean and made the
    render come out too light (the "brown pants render gold/beige" failure).
    The median is robust to those bright pixels and matches the garment's true
    dominant color."""
    try:
        img = Image.open(io.BytesIO(clean_bytes)).convert("RGB")
        img.thumbnail((160, 213))
        px = [c for c in img.getdata()
              if not (c[0] > 235 and c[1] > 235 and c[2] > 235)]
        if len(px) < 400:
            return None
        mid = len(px) // 2
        return tuple(sorted(c[i] for c in px)[mid] for i in range(3))
    except Exception:
        return None


def _match_garment_color(
    out_bytes: bytes,
    mask_bytes: bytes,
    ref: tuple[int, int, int],
    category: str = "top",
) -> bytes:
    """Deterministically shift a rendered garment region so its AVERAGE color
    equals the reference garment's ACTUAL color (the user's hard rule: only the
    garment's own color matters — never the base/scene). Per-pixel deviation
    from the region mean is preserved, so shading/folds survive while the color
    becomes the true garment color.

    The mask is ERODED inward before the shift for UPPER/OUTERWEAR garments:
    AutoMasker's upper mask is a torso silhouette WIDER than the actual body
    (and outerwear is dilated another +10% for the shoulders), so shifting that
    overhang to a solid color painted a bright ring around the body — the
    "giant halo". Bottoms are NOT eroded: their mask already hugs the legs."""
    img = Image.open(io.BytesIO(out_bytes)).convert("RGB")
    mask = Image.open(io.BytesIO(mask_bytes)).convert("L")
    if mask.size != img.size:
        mask = mask.resize(img.size, Image.LANCZOS)
    h = img.size[1]
    # Per-category overhang of the AutoMasker mask beyond the true garment,
    # as a fraction of image height. Eroding by this much (MinFilter =
    # morphological erosion, van-Herk so it's fast even at large kernel sizes)
    # pulls the shift inside the garment so it can't paint the background that
    # surrounds it. Bottoms erode 0 — the lower mask already fits the legs.
    erode_frac = {
        "outerwear": 0.12,  # upper silhouette overhang + 10% shoulder dilation
        "top": 0.06,
        "bra": 0.06,
        "dress": 0.06,
        "swimsuit": 0.06,
        "bottom": 0.0,
    }.get(category, 0.06)
    erode = int(h * erode_frac)
    if erode > 1:
        erode = erode if erode % 2 == 1 else erode + 1
        m = mask.filter(ImageFilter.MinFilter(erode))
    else:
        m = mask
    m = m.filter(ImageFilter.GaussianBlur(2))
    # current region mean (downscaled for speed)
    sm = img.resize((96, 128))
    sma = m.resize((96, 128))
    d = list(sm.getdata())
    ma = list(sma.getdata())
    sel = [d[i] for i in range(len(d)) if ma[i] > 128]
    if not sel:
        return out_bytes
    cur = tuple(int(sum(p[i] for p in sel) / len(sel)) for i in range(3))
    delta = (ref[0] - cur[0], ref[1] - cur[1], ref[2] - cur[2])
    if max(abs(v) for v in delta) < 3:
        return out_bytes  # already the right color
    bands = img.split()
    new_bands = []
    for band, dlt in zip(bands, delta):
        lut = [max(0, min(255, v + dlt)) for v in range(256)]
        new_bands.append(band.point(lut))
    shifted = Image.merge("RGB", new_bands)
    buf = io.BytesIO()
    Image.composite(shifted, img, m).save(buf, "PNG")
    return buf.getvalue()


def _merge_masks(masks: list[bytes]) -> bytes:
    """OR a list of garment masks into one union — the total region the clothes
    cover. Used for the final clothes-only composite: outside the union the
    ORIGINAL base photo wins, so nothing but the clothes ever changes."""
    union = None
    for m in masks:
        im = Image.open(io.BytesIO(m)).convert("L")
        union = im if union is None else ImageChops.lighter(union, im)
    buf = io.BytesIO()
    union.save(buf, "PNG")
    return buf.getvalue()


def _composite_masked(render_bytes: bytes, base_bytes: bytes, mask_bytes: bytes) -> bytes:
    """Force a render to stay INSIDE its mask: pixels where the mask is set come
    from the render (IDM's re-texture), everywhere else comes from the base
    (CatVTON's composite) UNCHANGED. This enforces the user's hard rule that
    IDM paints ONLY what CatVTON provides — IDM re-generates the whole frame
    through the UNet and can bleed its color/texture outside the mask (e.g. the
    navy bomber colored the green joggers in O-ZRQHH7). Clipping the IDM output
    to its mask makes bleed impossible: outside the garment, CatVTON's paint is
    the only thing that shows."""
    base = Image.open(io.BytesIO(base_bytes)).convert("RGB")
    render = Image.open(io.BytesIO(render_bytes)).convert("RGB")
    if render.size != base.size:
        render = render.resize(base.size, Image.LANCZOS)
    mask = Image.open(io.BytesIO(mask_bytes)).convert("L")
    if mask.size != base.size:
        mask = mask.resize(base.size, Image.LANCZOS)
    # hard garment edge: the mask IS the true silhouette now (no synthetic
    # overhang to hide), so a ~1px anti-alias edge is enough — a wide feather
    # is what smeared the boundary into the "fuzz border".
    mask = mask.filter(ImageFilter.GaussianBlur(1))
    buf = io.BytesIO()
    Image.composite(render, base, mask).save(buf, "PNG")
    return buf.getvalue()





async def run_tryon(person_bytes: bytes, garment: Garment, user_id: int) -> bytes:
    if not WORKFLOW_PATH.exists():
        raise ComfyUnavailable(
            "workflows/catvton.json missing — see workflows/README.md"
        )
    workflow = json.loads(WORKFLOW_PATH.read_text())
    garment_bytes = _load_garment_image(garment, user_id)
    cloth_type = CLOTH_TYPE.get(garment.category, "upper")
    person_bytes = _prep_person(person_bytes)

    async with httpx.AsyncClient(timeout=30) as client:
        person_name = await _upload(client, "person.png", person_bytes)
        garment_name = await _upload(client, "garment.png", garment_bytes)
        _wire_workflow(workflow, person_name, garment_name, cloth_type)
        prompt_id = await _submit(client, workflow)
        entry = await _poll(client, prompt_id)
        return await _fetch_output(client, entry)


def _load_garment_image(g: Garment, user_id: int) -> bytes:
    """Resolve the garment image file (any extension) — uses the recorded
    image_path if present, else globs data/wardrobe/<owner>/<gid>.*. Images
    live under the OWNER's dir, so g.user_id is used (the `user_id` arg is the
    person trying it on — kept for call-site clarity)."""
    d = Path(settings.data_dir) / "wardrobe" / str(g.user_id)
    candidate: Path | None = None
    if g.image_path:
        p = d / g.image_path
        if p.is_file():
            candidate = p
    if candidate is None and d.is_dir():
        for p in sorted(d.glob(f"{g.id}.*")):
            if p.is_file():
                candidate = p
                break
    if candidate is None:
        raise ComfyUnavailable(
            f"garment image missing for #{g.id} — add one in the Wardrobe tab "
            f"(upload or paste a product image link)"
        )
    return candidate.read_bytes()


def _prep_person(data: bytes) -> bytes:
    """Normalize the person photo for CatVTON so the model NEVER crops the head
    or sees the image sideways:

      * apply EXIF orientation (phone/DSLR shots store portrait as landscape +
        a rotation tag; without this CatVTON gets a sideways person → distorted
        proportions, the 'fatter' look)
      * letterbox onto the 768x1024 (3:4) canvas CatVTON center-crops to, so
        its internal resize_and_crop() becomes a no-op (padding, never cropping)

    The full body (head to feet) is always preserved; gray letterbox bars fill
    the remaining canvas just like CatVTON's own training/garment padding."""
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img).convert("RGB")
    canvas = Image.new("RGB", (CATVTON_W, CATVTON_H), (128, 128, 128))
    scale = min(CATVTON_W / img.width, CATVTON_H / img.height)
    img = img.resize(
        (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
        Image.LANCZOS,
    )
    canvas.paste(img, ((CATVTON_W - img.width) // 2, (CATVTON_H - img.height) // 2))
    buf = io.BytesIO()
    canvas.save(buf, "PNG")
    return buf.getvalue()


async def _upload(client: httpx.AsyncClient, name: str, data: bytes) -> str:
    r = await client.post(
        f"{settings.comfyui_url}/upload/image",
        files={"image": (name, data, "image/png")},
    )
    r.raise_for_status()
    return r.json()["name"]


def _wire_workflow(
    workflow: dict, person_name: str, garment_name: str, cloth_type: str
) -> None:
    """Point the workflow at the freshly-uploaded images + garment type."""
    n = NODE_IDS
    workflow[n["person_image"]]["inputs"]["image"] = person_name
    workflow[n["garment_image"]]["inputs"]["image"] = garment_name
    workflow[n["automasker"]]["inputs"]["cloth_type"] = cloth_type
    # random seed per request for variety (override via TRYON_SEED for reproducibility)
    seed = settings.tryon_seed if settings.tryon_seed is not None else random.randint(0, 2**31)
    workflow[n["catvton"]]["inputs"]["seed"] = seed


async def _submit(client: httpx.AsyncClient, workflow: dict) -> str:
    r = await client.post(f"{settings.comfyui_url}/prompt", json={"prompt": workflow})
    if r.status_code != 200:
        raise ComfyUnavailable(f"ComfyUI rejected prompt: {r.text[:300]}")
    return r.json()["prompt_id"]


async def _poll(client: httpx.AsyncClient, prompt_id: str, timeout: float = 240.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = await client.get(f"{settings.comfyui_url}/history/{prompt_id}")
        r.raise_for_status()
        entry = r.json().get(prompt_id)
        if entry:
            status = entry.get("status", {})
            if status.get("completed"):
                return entry
            if status.get("status_str") == "error":
                raise ComfyUnavailable(f"ComfyUI error: {status.get('messages')}")
        await asyncio.sleep(2)
    raise ComfyUnavailable(f"ComfyUI timeout after {timeout:.0f}s")


async def _fetch_output(client: httpx.AsyncClient, entry: dict) -> bytes:
    for node in entry.get("outputs", {}).values():
        for img in node.get("images", []):
            r = await client.get(
                f"{settings.comfyui_url}/view",
                params={
                    "filename": img["filename"],
                    "subfolder": img.get("subfolder", ""),
                    "type": img.get("type", "output"),
                },
            )
            r.raise_for_status()
            return r.content
    raise ComfyUnavailable("ComfyUI finished but produced no image")


async def _fetch_outputs(
    client: httpx.AsyncClient, entry: dict, wanted: set[str]
) -> dict[str, bytes]:
    """Fetch rendered images from SPECIFIC output node ids (e.g. the CatVTON
    render AND its AutoMasker area mask from one prompt).
    `entry["outputs"]` is keyed by node id."""
    out: dict[str, bytes] = {}
    for nid, node_out in entry.get("outputs", {}).items():
        if nid not in wanted:
            continue
        for img in node_out.get("images", []):
            r = await client.get(
                f"{settings.comfyui_url}/view",
                params={
                    "filename": img["filename"],
                    "subfolder": img.get("subfolder", ""),
                    "type": img.get("type", "output"),
                },
            )
            r.raise_for_status()
            out[nid] = r.content
            break
    return out
