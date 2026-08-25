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
import io
import json
import random
import time
from pathlib import Path

import httpx
from PIL import Image, ImageFilter, ImageOps

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


# --- multi-model try-on (dev-selectable backends) ---
# "catvton" is the fast/live default and always present. "idm_vton" / "flux_kontext"
# are higher-quality backends; they become usable once their workflow file exists
# in workflows/ AND the model is in TRYON_MODELS (see run_tryon_model).
MODEL_LABELS = {
    "catvton": "CatVTON (SD1.5, fast)",
    "idm_vton": "IDM-VTON (SDXL)",
    "flux_kontext": "FLUX Kontext (FLUX.1-dev)",
}
MODEL_WORKFLOWS = {
    "catvton": "catvton.json",
    "idm_vton": "idm_vton.json",
    "flux_kontext": "flux_kontext.json",
}


def _workflow_path(model: str) -> Path:
    return Path(__file__).parent / "workflows" / MODEL_WORKFLOWS.get(model, f"{model}.json")


def available_models() -> list[str]:
    """Enabled + workflow-present models, in TRYON_MODELS order. CatVTON is the
    guaranteed fallback (its workflow ships with the app)."""
    out = [m for m in settings.tryon_models if m in MODEL_LABELS and _workflow_path(m).exists()]
    if not out and _workflow_path("catvton").exists():
        out = ["catvton"]
    return out


async def run_tryon_model(
    model: str, person_bytes: bytes, garment: Garment, user_id: int
) -> bytes:
    """Render one garment with a named model backend. Unknown / unconfigured
    backends raise ComfyUnavailable with a clear message (surfaced per-model)."""
    if model == "catvton":
        return await run_tryon(person_bytes, garment, user_id)
    if model == "idm_vton":
        return await _run_idm_vton(person_bytes, garment, user_id)
    wf = _workflow_path(model)
    if not wf.exists():
        raise ComfyUnavailable(
            f"model '{model}' not configured — workflow {wf.name} missing "
            f"(see services/webapp/app/workflows/)"
        )
    raise ComfyUnavailable(
        f"model '{model}' has a workflow but no renderer wired yet in tryon.py"
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
    "lower": "a pair of pants (jeans) worn on the lower body — each leg covers one leg from the hips down to the ankles, with a waistband at the waist. It is NOT a dress and NOT a skirt.",
    "overall": "a one-piece garment (dress or overall) covering the torso and the legs",
}
_DEFAULT_GARMENT_DESC = "The garment shown in the reference image."


async def _run_idm_vton(
    person_bytes: bytes, garment: Garment, user_id: int, seed: int | None = None
) -> bytes:
    """IDM-VTON (SDXL) try-on of ONE garment onto a person image."""
    render, _ = await _idm_render_mask(person_bytes, garment, user_id, seed)
    return render


async def _idm_render_mask(
    person_bytes: bytes, garment: Garment, user_id: int, seed: int | None = None
) -> tuple[bytes, float | None]:
    """IDM-VTON (SDXL) try-on of one garment, returning (render, waist) so an
    outfit can composite renders. Two passes, never sharing VRAM:
      pass 0 — free any resident models (CatVTON / a previous IDM pipeline).
      pass 1 — CatVTON's LoadAutoMasker/AutoMasker builds the garment mask
               (~2GB); a bottom garment's mask is reshaped into PANTS (two
               legs) so the jeans are never painted as a full-length dress.
      pass 2 — the IDM-VTON pipeline (~13GB) renders with the pre-made mask +
               controlnet_aux DensePose pose image.
      pass 3 — free the pipeline again so the next render's mask pass fits.
    Returns (render_bytes, waist_fraction) — waist is None unless this was a
    bottom (then it is the row fraction where the pants begin, used for the
    outfit composite's waist seam)."""
    if not IDM_WORKFLOW_PATH.exists() or not IDM_MASK_WORKFLOW_PATH.exists():
        raise ComfyUnavailable("workflows/idm_vton*.json missing — see workflows/README.md")
    person_bytes = _prep_person(person_bytes)

    async with httpx.AsyncClient(timeout=30) as client:
        await _free_models(client)  # clear CatVTON / previous IDM pipeline first
        person_name = await _upload(client, "person.png", person_bytes)
        render, waist = await _render_idm_garment(client, person_name, garment, user_id, seed)
        await _free_models(client)
        return render, waist


async def _render_idm_garment(
    client: httpx.AsyncClient,
    person_name: str,
    garment: Garment,
    user_id: int,
    seed: int | None,
) -> tuple[bytes, float | None]:
    """Two-pass IDM render of ONE garment onto an already-uploaded person
    (uploaded once and shared across an outfit's garments). Bottoms get a
    PANTS-shaped mask + a clear "pants" description so they read as pants, not
    a dress. Returns (render_bytes, waist_fraction_or_None)."""
    garment_bytes = _load_garment_image(garment, user_id)
    cloth_type = CLOTH_TYPE.get(garment.category, "upper")
    description = _GARMENT_DESCRIPTIONS.get(cloth_type, _DEFAULT_GARMENT_DESC)
    # pass 1: AutoMasker mask — run BEFORE the pipeline loads
    mask_bytes = await _automasker(client, person_name, cloth_type)
    waist = None
    if cloth_type == "lower":
        mask_bytes, waist = _to_pants_mask(mask_bytes)
    elif cloth_type == "upper":
        # On a one-piece dress the 'upper' mask covers the WHOLE dress, which
        # makes IDM paint the top as a dress-length garment (the 'grey
        # long-sleeve as a dress' failure). Trim it at the waist so the top
        # renders with its hem at the waist — a proper TOP, not a dress.
        mask_bytes = _to_top_mask(mask_bytes, _waist_fraction(mask_bytes))
    # pass 2: IDM-VTON with the pre-made mask + DensePose pose
    render = await _idm_render(client, person_name, garment_bytes, mask_bytes, seed, description)
    return render, waist


async def _automasker(client: httpx.AsyncClient, person_name: str, cloth_type: str) -> bytes:
    """Pass 1 of IDM: CatVTON's LoadAutoMasker/AutoMasker builds the garment
    mask (cloth_type from the category); ~2GB."""
    mask_workflow = json.loads(IDM_MASK_WORKFLOW_PATH.read_text())
    mn = IDM_MASK_NODE_IDS
    mask_workflow[mn["person_image"]]["inputs"]["image"] = person_name
    mask_workflow[mn["automasker"]]["inputs"]["cloth_type"] = cloth_type
    mask_entry = await _poll(client, await _submit(client, mask_workflow))
    return await _fetch_output(client, mask_entry)


async def _idm_render(
    client: httpx.AsyncClient,
    person_name: str,
    garment_bytes: bytes,
    mask_bytes: bytes,
    seed: int | None,
    garment_description: str,
) -> bytes:
    """Pass 2 of IDM: the ~13GB pipeline renders the garment onto the person
    using the pre-made mask + DensePose pose. The description gives the model
    clear instructions about what kind of garment this is (top vs pants)."""
    mask_name = await _upload(client, "mask.png", mask_bytes)
    workflow = json.loads(IDM_WORKFLOW_PATH.read_text())
    garment_name = await _upload(client, "garment.png", garment_bytes)
    n = IDM_NODE_IDS
    workflow[n["person_image"]]["inputs"]["image"] = person_name
    workflow[n["garment_image"]]["inputs"]["image"] = garment_name
    workflow[n["mask_image"]]["inputs"]["image"] = mask_name
    workflow[n["idm"]]["inputs"]["garment_description"] = garment_description
    if seed is None:
        seed = settings.tryon_seed if settings.tryon_seed is not None else random.randint(0, 2**31)
    workflow[n["idm"]]["inputs"]["seed"] = seed
    entry = await _poll(client, await _submit(client, workflow))
    return await _fetch_output(client, entry)


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
    """IDM-VTON complete outfit (multiple garments, e.g. top + bottom).

    Strategy — HYBRID-COMPOSITE (user-approved, quality-tuned 2026-08-25):
      0. CatVTON chains ALL garments onto the original person FIRST. CatVTON is
         a true inpainter: it establishes the correct garment BOUNDARIES — real
         full-length jeans even on a shorts-wearing base (which IDM alone can't
         do, verified).
      1. IDM re-wears EACH garment as a SINGLE pass onto that CatVTON base —
         NOT chained. Chaining re-generates the whole body at every step and
         compounds blur (measured edge_std 29 vs 47.9 for single-pass at
         768x1024). Single passes stay sharp and each garment warps onto the
         already-correct fabric.
      2. Composite the top + bottom renders at the bottom's waist (feathered,
         `_composite_at_waist`) — no chain-blur, no waist seam.
      Result: CatVTON-correct boundaries with sharp IDM texture.
    """
    if not garments:
        raise ComfyUnavailable("no garments to render")
    if len(garments) == 1:
        return await _run_idm_vton(person_bytes, garments[0], user_id, seed)
    if not IDM_WORKFLOW_PATH.exists() or not IDM_MASK_WORKFLOW_PATH.exists():
        raise ComfyUnavailable("workflows/idm_vton*.json missing — see workflows/README.md")
    if seed is None:
        seed = settings.tryon_seed if settings.tryon_seed is not None else random.randint(0, 2**31)
    # step 0 — CatVTON establishes correct boundaries (true inpainter).
    cat_base = person_bytes
    for g in garments:
        cat_base = await run_tryon(cat_base, g, user_id)
    person_bytes = _prep_person(cat_base)

    async with httpx.AsyncClient(timeout=30) as client:
        await _free_models(client)
        person_name = await _upload(client, "person.png", person_bytes)
        # step 1 — one SHARP single-pass IDM render per garment onto the SAME
        # CatVTON base (no chaining — that's what made the output fuzzy).
        renders: list[tuple[bytes, float | None]] = []
        for g in garments:
            render, waist = await _render_idm_garment(client, person_name, g, user_id, seed)
            renders.append((render, waist))
            await _free_models(client)
        result = renders[0][0]
        # step 2 — composite top + bottom at the bottom's waist (feathered seam).
        if len(renders) == 2 and renders[1][1] is not None:
            result = _composite_at_waist(renders[0][0], renders[1][0], renders[1][1])
        return result


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


def _to_pants_mask(mask_bytes: bytes) -> tuple[bytes, float]:
    """Reshape a 'lower' AutoMasker mask into a PANTS shape: a solid waistband +
    two straight leg columns that separate as they go down, so IDM paints the
    jeans as two legs instead of a full-length dress.

    Two "IDM renders shorts/skirt" failures this fixes:
      * dress base — the 'lower' mask is a wide A-line blob covering the whole
        dress; the waistband + two legs reshape it into pants geometry.
      * shorts/skirt base — AutoMasker's 'lower' mask only covers the garment
        the person is CURRENTLY wearing, so it stops at mid-thigh and IDM paints
        SHORTS. We pin the waistband to the TOP of the lower garment (the true
        waist on separates) and extend the legs down to the ankles (near the
        bottom of the frame) so jeans always render FULL-length.
    Returns the pants mask + the waist row fraction."""
    m = Image.open(io.BytesIO(mask_bytes)).convert("L")
    w, h = m.size
    rows = []
    for y in range(h):
        xs = [x for x in range(w) if m.getpixel((x, y)) > 128]
        if xs:
            rows.append((y, min(xs), max(xs)))
    if not rows:
        return mask_bytes, 0.5
    y_top = rows[0][0]
    y_bot = rows[-1][0]
    waist = _waist_fraction(mask_bytes)
    # Separates (shorts/pants): the lower garment's TOP is the natural waist —
    # _waist_fraction's 42-62% band lands mid-shorts. A dress starts high
    # (shoulders), so a low mask start means separates.
    if (y_top / h) > 0.30:
        waist = (y_top + (y_bot - y_top) * 0.04) / h
    wy = int(waist * h)
    row_xs = [x for x in range(w) if m.getpixel((x, wy)) > 128]
    if row_xs:
        cx0, cx1 = min(row_xs), max(row_xs)
    else:
        cx0, cx1 = rows[0][1], rows[0][2]
    cy = (cx0 + cx1) / 2
    leg_w = max(70, min(120, int((cx1 - cx0) * 0.5)))
    # Extend the legs to the ankles: the mask stops at the current garment
    # (shorts → mid-thigh), so without this the jeans would render as shorts.
    # When the base already wears full-length pants (or a dress) this is a no-op.
    y_bot = max(y_bot, int(h * 0.96))
    out = Image.new("L", (w, h), 0)
    for y in range(max(0, wy - 6), min(h, wy + 12)):  # waistband
        for x in range(max(0, int(cy - leg_w)), min(w, int(cy + leg_w) + 1)):
            out.putpixel((x, y), 255)
    for y in range(wy + 12, y_bot + 1):  # two legs, gap widening downward
        t = (y - (wy + 12)) / max(1, y_bot - (wy + 12))
        gap = int(26 + 50 * t)
        for x in range(max(0, int(cy - leg_w)), min(w, int(cy - gap / 2))):
            out.putpixel((x, y), 255)
        for x in range(max(0, int(cy + gap / 2)), min(w, int(cy + leg_w) + 1)):
            out.putpixel((x, y), 255)
    buf = io.BytesIO()
    out.save(buf, "PNG")
    return buf.getvalue(), waist


def _composite_at_waist(top_bytes: bytes, bottom_bytes: bytes, waist_fraction: float) -> bytes:
    """Blend the top render (above the waist) with the bottom render (below the
    waist). The split follows the pants' waistband row, feathered over ~3% of
    the image height so there is no hard line. Pure-PIL Image.composite."""
    a = Image.open(io.BytesIO(top_bytes)).convert("RGB")
    b = Image.open(io.BytesIO(bottom_bytes)).convert("RGB").resize(a.size, Image.LANCZOS)
    w, h = a.size
    wy = int(waist_fraction * h)
    feather = max(12, int(h * 0.03))
    mask = Image.new("L", (w, h), 0)
    px = mask.load()
    for y in range(h):
        if y <= wy - feather:
            v = 255
        elif y >= wy + feather:
            v = 0
        else:
            v = int(255 * (1 - (y - (wy - feather)) / (2 * feather)))
        for x in range(w):
            px[x, y] = v
    mask = mask.filter(ImageFilter.GaussianBlur(max(3, feather // 4)))
    buf = io.BytesIO()
    # a (top) wins where the mask is white (above waist); b (bottom) below.
    Image.composite(a, b, mask).save(buf, "PNG")
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
