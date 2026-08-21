"""ComfyUI-based image editor for the try-on chat.

Engine abstraction: `run_edit()` dispatches to whatever engine
`settings.editor_engine` names, so a heavier "swap" editor (e.g. FLUX.1-Kontext
GGUF, which can't sit resident with CatVTON) can be added later behind the same
endpoint — no webapp changes needed, just add the engine + workflow here.
"""
from __future__ import annotations

import asyncio
import io
import json
import random
from pathlib import Path

import httpx
from PIL import Image

from .config import settings
from .tryon import ComfyUnavailable, _fetch_output, _poll, _submit, _upload

WORKFLOW_PATH = Path(__file__).parent / "workflows" / "ip2p.json"

# node ids in workflows/ip2p.json
NODE_IDS = {"image": "5", "positive": "6", "sampler": "3"}

# InstructPix2Pix is a 512×512 model — feeding it a tall full-body render at
# full rewrite strength produces heavy distortion. We pad the input to a square
# it can handle and crop the output back to the original aspect ratio.
EDIT_SIZE = 512
PAD_COLOR = (120, 120, 120)


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
    square, orig_size = _pad_to_square(image_bytes)
    async with httpx.AsyncClient(timeout=30) as client:
        image_name = await _upload(client, "edit_base.png", square)
        _wire_workflow(workflow, image_name, prompt)
        prompt_id = await _submit(client, workflow)
        entry = await _poll(client, prompt_id)
        edited = await _fetch_output(client, entry)
    return _restore_aspect(edited, orig_size)


def _pad_to_square(data: bytes) -> tuple[bytes, tuple[int, int]]:
    """Center-pad the image into a square canvas (no distortion)."""
    img = Image.open(io.BytesIO(data)).convert("RGB")
    orig = img.size
    scale = EDIT_SIZE / max(img.size)
    img = img.resize(
        (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
        Image.LANCZOS,
    )
    canvas = Image.new("RGB", (EDIT_SIZE, EDIT_SIZE), PAD_COLOR)
    canvas.paste(img, ((EDIT_SIZE - img.width) // 2, (EDIT_SIZE - img.height) // 2))
    buf = io.BytesIO()
    canvas.save(buf, "PNG")
    return buf.getvalue(), orig


def _restore_aspect(data: bytes, orig: tuple[int, int]) -> bytes:
    """Center-crop the square output back to the original aspect ratio."""
    img = Image.open(io.BytesIO(data)).convert("RGB")
    w, h = img.size
    ratio = orig[0] / orig[1]
    if abs(w / h - ratio) < 0.02:
        return data
    if ratio < 1:  # tall original → crop the horizontal padding
        new_w = min(w, round(h * ratio))
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    else:  # wide original → crop the vertical padding
        new_h = min(h, round(w / ratio))
        top = (h - new_h) // 2
        img = img.crop((0, top, w, top + new_h))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _wire_workflow(workflow: dict, image_name: str, prompt: str) -> None:
    """Point the IP2P workflow at the uploaded image + the instruction prompt."""
    n = NODE_IDS
    workflow[n["image"]]["inputs"]["image"] = image_name
    workflow[n["positive"]]["inputs"]["text"] = prompt
    seed = settings.tryon_seed if settings.tryon_seed is not None else random.randint(0, 2**31)
    workflow[n["sampler"]]["inputs"]["seed"] = seed
