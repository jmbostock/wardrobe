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
# A person photo where the subject is wearing SEPARATES (top + bottom) is a far
# better try-on base for a top+bottom outfit than a one-piece-dress photo: on a
# dress the AutoMasker "lower" mask covers the WHOLE body, so the model paints
# the jeans over the whole dress silhouette and they come out as a SKIRT; on a
# separates photo the mask is pants-length and the jeans render as PANTS
# (verified empirically — photo 29 vs photo 32). `photo_style` classifies a
# saved photo with the AutoMasker "lower" mask (cached per file mtime), and the
# try-on route picks the best base for the look.
_PHOTO_STYLE_CACHE: dict[str, str] = {}


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


async def photo_style(user_id: int, photo_id: int) -> str:
    """Classify a saved person photo as 'dress' or 'separates', cached per
    (user, photo, file mtime). Runs the AutoMasker 'lower' mask via ComfyUI."""
    from .photos import photo_bytes, photo_path

    try:
        mtime = int(photo_path(user_id, photo_id).stat().st_mtime)
    except Exception:  # noqa: BLE001
        mtime = 0
    key = f"{user_id}:{photo_id}:{mtime}"
    cached = _PHOTO_STYLE_CACHE.get(key)
    if cached:
        return cached
    data = photo_bytes(user_id, photo_id)
    async with httpx.AsyncClient(timeout=30) as client:
        await _free_models(client)
        person_name = await _upload(client, "style.png", data)
        mask = await _automasker(client, person_name, "lower")
        await _free_models(client)
    style = photo_style_from_mask(mask)
    _PHOTO_STYLE_CACHE[key] = style
    return style


async def _run_idm_vton_outfit(
    person_bytes: bytes, garments: list[Garment], user_id: int, seed: int | None = None
) -> bytes:
    """IDM-VTON complete outfit (multiple garments, e.g. top + bottom).

    Strategy — CHAINED, with a PANTS-shaped mask for bottoms:
      1. render the first garment onto the ORIGINAL person (a top, or a dress).
      2. for each following garment, CHAIN onto that render, but a bottom uses
         a PANTS-shaped mask (AutoMasker 'lower' reshaped into a waistband + two
         legs) + a clear "pants" description. Because that mask only covers the
         legs, IDM keeps the top from the input (no more dropped top) and paints
         the jeans as proper PANTS (no more denim dress). Result is ONE cohesive
         render — no composite seam.
    """
    if not garments:
        raise ComfyUnavailable("no garments to render")
    if len(garments) == 1:
        return await _run_idm_vton(person_bytes, garments[0], user_id, seed)
    if not IDM_WORKFLOW_PATH.exists() or not IDM_MASK_WORKFLOW_PATH.exists():
        raise ComfyUnavailable("workflows/idm_vton*.json missing — see workflows/README.md")
    if seed is None:
        seed = settings.tryon_seed if settings.tryon_seed is not None else random.randint(0, 2**31)
    person_bytes = _prep_person(person_bytes)

    async with httpx.AsyncClient(timeout=30) as client:
        await _free_models(client)
        person_name = await _upload(client, "person.png", person_bytes)
        # garment 0 — onto the original person (a top, or a one-piece)
        current, _ = await _render_idm_garment(client, person_name, garments[0], user_id, seed)
        await _free_models(client)
        # garments 1..n — chain onto the previous render; bottoms automatically
        # get the pants mask + description inside _render_idm_garment.
        for g in garments[1:]:
            chained_name = await _upload(client, "chained.png", current)
            current, _ = await _render_idm_garment(client, chained_name, g, user_id, seed)
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


def _to_pants_mask(mask_bytes: bytes) -> tuple[bytes, float]:
    """Reshape a 'lower' AutoMasker mask (a wide A-line dress blob when the
    source photo is a dress) into a PANTS shape: a solid waistband + two
    straight leg columns that separate as they go down. This gives IDM the
    geometry of "pants", so the jeans are painted as two legs instead of a
    full-length dress. Returns the pants mask + the waist row fraction."""
    m = Image.open(io.BytesIO(mask_bytes)).convert("L")
    w, h = m.size
    waist = _waist_fraction(mask_bytes)
    wy = int(waist * h)
    row_xs = [x for x in range(w) if m.getpixel((x, wy)) > 128]
    if row_xs:
        cx0, cx1 = min(row_xs), max(row_xs)
    else:
        rows = []
        for y in range(h):
            xs = [x for x in range(w) if m.getpixel((x, y)) > 128]
            if xs:
                rows.append((y, min(xs), max(xs)))
        if not rows:
            return mask_bytes, 0.5
        cx0, cx1 = rows[0][1], rows[0][2]
    cy = (cx0 + cx1) / 2
    leg_w = max(70, min(120, int((cx1 - cx0) * 0.5)))
    y_bot = max((y for y in range(h) if any(m.getpixel((x, y)) > 128 for x in range(w))), default=wy)
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
