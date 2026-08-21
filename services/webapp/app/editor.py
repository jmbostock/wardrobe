"""ComfyUI-based image editor for the try-on chat.

Engine abstraction: `run_edit()` dispatches to whatever engine
`settings.editor_engine` names, so a heavier "swap" editor (e.g. FLUX.1-Kontext
GGUF, which can't sit resident with CatVTON) can be added later behind the same
endpoint — no webapp changes needed, just add the engine + workflow here.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

import httpx

from .config import settings
from .tryon import ComfyUnavailable, _fetch_output, _poll, _submit, _upload

WORKFLOW_PATH = Path(__file__).parent / "workflows" / "ip2p.json"

# node ids in workflows/ip2p.json
NODE_IDS = {"image": "5", "positive": "6", "sampler": "3"}


async def run_edit(image_bytes: bytes, prompt: str) -> bytes:
    """Apply an instruction edit to an image. Returns the edited PNG bytes."""
    engine = (settings.editor_engine or "ip2p").lower()
    if engine == "ip2p":
        return await _run_ip2p(image_bytes, prompt)
    # Future engines: "fluxkontext" (swap-required) would be added here.
    raise ComfyUnavailable(f"editor engine {engine!r} is not available")


async def _run_ip2p(image_bytes: bytes, prompt: str) -> bytes:
    if not WORKFLOW_PATH.exists():
        raise ComfyUnavailable("workflows/ip2p.json missing — editor not installed")
    workflow = json.loads(WORKFLOW_PATH.read_text())
    async with httpx.AsyncClient(timeout=30) as client:
        image_name = await _upload(client, "edit_base.png", image_bytes)
        _wire_workflow(workflow, image_name, prompt)
        prompt_id = await _submit(client, workflow)
        entry = await _poll(client, prompt_id)
        return await _fetch_output(client, entry)


def _wire_workflow(workflow: dict, image_name: str, prompt: str) -> None:
    """Point the IP2P workflow at the uploaded image + the instruction prompt."""
    n = NODE_IDS
    workflow[n["image"]]["inputs"]["image"] = image_name
    workflow[n["positive"]]["inputs"]["text"] = prompt
    seed = settings.tryon_seed if settings.tryon_seed is not None else random.randint(0, 2**31)
    workflow[n["sampler"]]["inputs"]["seed"] = seed
