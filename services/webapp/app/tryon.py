"""ComfyUI / CatVTON client for virtual try-on.

ComfyUI is internal-only (comfyui:8188). Flow:
  1. upload person + garment images
  2. wire the CatVTON workflow JSON (see workflows/README.md)
  3. submit to /prompt, poll /history/{id}
  4. return the rendered image bytes

Phase 2 TODO: once the official CatVTON ComfyUI workflow is committed to
app/workflows/catvton.json, fill NODE_IDS and _wire_workflow().
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx

from .config import settings
from .wardrobe import Garment

WORKFLOW_PATH = Path(__file__).parent / "workflows" / "catvton.json"

# Semantic role -> node id in catvton.json (fill in at Phase 2)
NODE_IDS = {
    "person_image": "??",
    "garment_image": "??",
    "ckpt": "??",
    "output_image": "??",
}


class ComfyUnavailable(Exception):
    """ComfyUI is missing, errored, or timed out — surfaced as HTTP 503."""


async def run_tryon(person_bytes: bytes, garment: Garment, user_id: int) -> bytes:
    if not WORKFLOW_PATH.exists():
        raise ComfyUnavailable(
            "workflows/catvton.json missing — see workflows/README.md (Phase 2)"
        )
    workflow = json.loads(WORKFLOW_PATH.read_text())
    garment_bytes = _load_garment_image(garment, user_id)

    async with httpx.AsyncClient(timeout=30) as client:
        person_name = await _upload(client, "person.png", person_bytes)
        garment_name = await _upload(client, "garment.png", garment_bytes)
        _wire_workflow(workflow, person_name, garment_name)
        prompt_id = await _submit(client, workflow)
        entry = await _poll(client, prompt_id)
        return await _fetch_output(client, entry)


def _load_garment_image(g: Garment, user_id: int) -> bytes:
    path = Path(settings.data_dir) / "wardrobe" / str(user_id) / f"{g.id}.png"
    if not path.exists():
        raise ComfyUnavailable(
            f"garment image missing: {path} — drop a photo at "
            f"data/wardrobe/{user_id}/{g.id}.png"
        )
    return path.read_bytes()


async def _upload(client: httpx.AsyncClient, name: str, data: bytes) -> str:
    r = await client.post(
        f"{settings.comfyui_url}/upload/image",
        files={"image": (name, data, "image/png")},
    )
    r.raise_for_status()
    return r.json()["name"]


def _wire_workflow(workflow: dict, person_name: str, garment_name: str) -> None:
    # TODO(phase 2): set person/garment LoadImage node inputs to the uploaded
    # names, and point the checkpoint node at a model in data/comfyui/models.
    ...


async def _submit(client: httpx.AsyncClient, workflow: dict) -> str:
    r = await client.post(f"{settings.comfyui_url}/prompt", json={"prompt": workflow})
    r.raise_for_status()
    return r.json()["prompt_id"]


async def _poll(client: httpx.AsyncClient, prompt_id: str, timeout: float = 120.0) -> dict:
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
        await asyncio.sleep(1)
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
