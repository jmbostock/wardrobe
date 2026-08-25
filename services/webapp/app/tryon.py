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
    idm_vton — renders each garment onto the ORIGINAL person and composites by
               mask; chaining render-onto-render DROPS the first garment because
               IDM regenerates the whole body each pass (verified empirically).
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


async def _run_idm_vton(
    person_bytes: bytes, garment: Garment, user_id: int, seed: int | None = None
) -> bytes:
    """IDM-VTON (SDXL) try-on of ONE garment onto a person image. For multi-
    garment outfits use `_run_idm_vton_outfit` (chaining render-onto-render
    DROPS the first garment — IDM regenerates the whole body each pass)."""
    render, _ = await _idm_render_mask(person_bytes, garment, user_id, seed)
    return render


async def _idm_render_mask(
    person_bytes: bytes, garment: Garment, user_id: int, seed: int | None = None
) -> tuple[bytes, bytes]:
    """IDM-VTON (SDXL) try-on of one garment, returning (render, mask) so the
    caller can composite multi-garment outfits. Two passes, never sharing VRAM:
      pass 0 — free any resident models (CatVTON / a previous IDM pipeline).
      pass 1 — CatVTON's LoadAutoMasker/AutoMasker builds the garment mask
               (cloth_type from the category); ~2GB.
      pass 2 — the IDM-VTON pipeline (~13GB on the 5060 Ti) renders using the
               pre-made mask + controlnet_aux DensePose pose image.
      pass 3 — free the pipeline again so the next render's mask pass fits.
    Heavier than CatVTON — meant for the dev / batch (overnight) path where the
    vision llama-server + ollama are stopped first."""
    if not IDM_WORKFLOW_PATH.exists() or not IDM_MASK_WORKFLOW_PATH.exists():
        raise ComfyUnavailable("workflows/idm_vton*.json missing — see workflows/README.md")
    person_bytes = _prep_person(person_bytes)

    async with httpx.AsyncClient(timeout=30) as client:
        await _free_models(client)  # clear CatVTON / previous IDM pipeline first
        person_name = await _upload(client, "person.png", person_bytes)
        garment_bytes = _load_garment_image(garment, user_id)
        cloth_type = CLOTH_TYPE.get(garment.category, "upper")
        out, mask = await _idm_render_mask_for(client, person_name, garment_bytes, cloth_type, seed)
        await _free_models(client)
        return out, mask


async def _idm_render_mask_for(
    client: httpx.AsyncClient,
    person_name: str,
    garment_bytes: bytes,
    cloth_type: str,
    seed: int | None,
) -> tuple[bytes, bytes]:
    """The two IDM passes against an already-uploaded person (the person is
    uploaded once and shared across the garments of one outfit). Returns
    (render_bytes, mask_bytes)."""
    # pass 1: garment mask (AutoMasker) — run BEFORE the pipeline loads
    mask_workflow = json.loads(IDM_MASK_WORKFLOW_PATH.read_text())
    mn = IDM_MASK_NODE_IDS
    mask_workflow[mn["person_image"]]["inputs"]["image"] = person_name
    mask_workflow[mn["automasker"]]["inputs"]["cloth_type"] = cloth_type
    mask_entry = await _poll(client, await _submit(client, mask_workflow))
    mask_bytes = await _fetch_output(client, mask_entry)
    mask_name = await _upload(client, "mask.png", mask_bytes)
    # pass 2: IDM-VTON with the pre-made mask + DensePose pose
    workflow = json.loads(IDM_WORKFLOW_PATH.read_text())
    garment_name = await _upload(client, "garment.png", garment_bytes)
    n = IDM_NODE_IDS
    workflow[n["person_image"]]["inputs"]["image"] = person_name
    workflow[n["garment_image"]]["inputs"]["image"] = garment_name
    workflow[n["mask_image"]]["inputs"]["image"] = mask_name
    if seed is None:
        seed = settings.tryon_seed if settings.tryon_seed is not None else random.randint(0, 2**31)
    workflow[n["idm"]]["inputs"]["seed"] = seed
    entry = await _poll(client, await _submit(client, workflow))
    out = await _fetch_output(client, entry)
    return out, mask_bytes


async def _run_idm_vton_outfit(
    person_bytes: bytes, garments: list[Garment], user_id: int, seed: int | None = None
) -> bytes:
    """IDM-VTON complete outfit (multiple garments, e.g. top + bottom).

    IDM-VTON CANNOT be chained render-onto-render: when the 2nd garment's pass
    takes the 1st render as its "person", the model re-generates the WHOLE body
    and drops the previously-applied garment (verified: a chained top+jeans
    render lost the grey top). Instead we render EACH garment onto the ORIGINAL
    person photo (same pose + same seed → the bodies align), keep the AutoMasker
    mask per garment, then composite: each later garment wins only inside its
    own mask region, feathered at the boundary so there is no hard seam.
    Garment ORDER doesn't matter for upper/lower (non-overlapping regions)."""
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
        renders: list[bytes] = []
        masks: list[bytes] = []
        for g in garments:
            garment_bytes = _load_garment_image(g, user_id)
            cloth_type = CLOTH_TYPE.get(g.category, "upper")
            render, mask = await _idm_render_mask_for(
                client, person_name, garment_bytes, cloth_type, seed
            )
            renders.append(render)
            masks.append(mask)
            await _free_models(client)  # so the next garment's mask pass fits
        return _composite_by_mask(renders, masks)


def _composite_by_mask(renders: list[bytes], masks: list[bytes]) -> bytes:
    """Merge per-garment IDM renders using their AutoMasker masks.

    The FIRST render is the base; each later render replaces it ONLY inside
    that garment's mask region (white = the garment area — the same polarity
    AutoMasker feeds CatVTON/IDM), with the mask Gaussian-blurred so the
    boundary feathers instead of a hard seam. All renders are the same size
    (the IDM workflow's 640x896); masks are resized to match. Pure-PIL
    (Image.composite is exactly the soft alpha blend we need)."""
    base = Image.open(io.BytesIO(renders[0])).convert("RGB")
    for render, mask in zip(renders[1:], masks[1:]):
        m = Image.open(io.BytesIO(mask)).convert("L")
        m = m.resize(base.size, Image.BILINEAR)
        m = m.filter(ImageFilter.GaussianBlur(8))  # feather the seam
        img = Image.open(io.BytesIO(render)).convert("RGB")
        img = img.resize(base.size, Image.LANCZOS)
        base = Image.composite(img, base, m)
    buf = io.BytesIO()
    base.save(buf, "PNG")
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
