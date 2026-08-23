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
from PIL import Image, ImageOps

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
    image_path if present, else globs data/wardrobe/<uid>/<gid>.*."""
    d = Path(settings.data_dir) / "wardrobe" / str(user_id)
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
