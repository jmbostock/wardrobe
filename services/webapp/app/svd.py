"""SVD (Stable Video Diffusion) client — animates a try-on still into a short clip.

Non-blocking by design: `submit_svd()` uploads the image, submits the workflow
to ComfyUI and returns the prompt id immediately. ComfyUI queues the job, so
other renders (try-on / further clips) can be submitted while it runs. The
webapp tracks the prompt in the `clips` table and the frontend polls
`GET /api/clips/{id}`; `check_svd()` is the one-shot status + fetch.

SVD is a 576x1024 model; the try-on renders are portrait so we pad to a square
SVD can handle and crop the animated webp back to the original aspect ratio.
"""
from __future__ import annotations

import asyncio
import io
import json
import random
import time
from pathlib import Path

import httpx
from PIL import Image

from .config import settings
from .tryon import ComfyUnavailable, _fetch_output, _submit, _upload

WORKFLOW_PATH = Path(__file__).parent / "workflows" / "svd.json"

# node ids in workflows/svd.json
NODE_IDS = {"image": "2", "sampler": "5"}

# SVD native portrait resolution (matches the workflow).
MODEL_W = 576
MODEL_H = 1024

# Fit the subject inside this fraction of the canvas so SVD has breathing room
# (it can drift/pan without pushing the head out of frame).
FIT_SCALE = 0.80
# Extra headroom at the top (the face sits near the top of a full-body still).
TOP_MARGIN_FRAC = 0.13


async def submit_svd(image_bytes: bytes) -> str:
    """Upload the still and submit the SVD workflow. Returns the prompt id
    (ComfyUI queues it — the caller doesn't wait)."""
    if not WORKFLOW_PATH.exists():
        raise ComfyUnavailable("workflows/svd.json missing — SVD not installed")
    workflow = json.loads(WORKFLOW_PATH.read_text())
    canvas = _letterbox(image_bytes)
    async with httpx.AsyncClient(timeout=30) as client:
        image_name = await _upload(client, "svd_base.png", canvas)
        _wire_workflow(workflow, image_name)
        return await _submit(client, workflow)


async def check_svd(prompt_id: str) -> tuple[str, bytes | None]:
    """One-shot poll. Returns ("done", webp_bytes) when complete, ("running",
    None) while queued/executing, or raises ComfyUnavailable on error/timeout."""
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{settings.comfyui_url}/history/{prompt_id}")
        r.raise_for_status()
        entry = r.json().get(prompt_id)
        if not entry:
            return "running", None
        status = entry.get("status", {})
        if status.get("completed"):
            return "done", await _fetch_output(client, entry)
        if status.get("status_str") == "error":
            raise ComfyUnavailable(f"SVD error: {status.get('messages')}")
        return "running", None


def _letterbox(data: bytes) -> bytes:
    """Place the still on the 576x1024 SVD canvas with margins so the subject
    (especially the face at the top) can't be cut off by SVD's motion/pan."""
    img = Image.open(io.BytesIO(data)).convert("RGB")
    canvas = Image.new("RGB", (MODEL_W, MODEL_H), (120, 120, 120))
    max_w = MODEL_W * FIT_SCALE
    max_h = MODEL_H * FIT_SCALE
    scale = min(max_w / img.width, max_h / img.height)
    img = img.resize(
        (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
        Image.LANCZOS,
    )
    # Center horizontally; add extra room at the top for the head.
    x = (MODEL_W - img.width) // 2
    top_margin = int(MODEL_H * TOP_MARGIN_FRAC)
    y = min(top_margin, MODEL_H - img.height - 8)  # never overflow the bottom
    canvas.paste(img, (x, y))
    buf = io.BytesIO()
    canvas.save(buf, "PNG")
    return buf.getvalue()


def _wire_workflow(workflow: dict, image_name: str) -> None:
    n = NODE_IDS
    workflow[n["image"]]["inputs"]["image"] = image_name
    seed = settings.tryon_seed if settings.tryon_seed is not None else random.randint(0, 2**31)
    workflow[n["sampler"]]["inputs"]["seed"] = seed
