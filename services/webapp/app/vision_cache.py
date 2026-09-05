"""Upload-time vision cache — precompute + store what garments and base photos
ARE so base picking never needs a live vision call.

Two things are cached (each computed ONCE, at upload / nightly batch, then read
from the DB at request time):

  garments.vision_type / vision_desc — what the garment is (shorts|pants|dress|
        skirt|top|outerwear|other) + a stylist one-liner (also used to seed the
        IDM garment_description so the try-on composite is tied to the garment).
  photos.vision_type — what the person is wearing in the base photo
        (dress|shorts|pants|unknown), from the deterministic mask+skin check.

FashionCLIP embeddings for both garments AND photos are written by
scripts/rec_build.py on the GPU host (202) in the same nightly rec_weekly.sh
pass — see that script; this module only handles the vision classification cache
(which needs the vision model / ComfyUI, both reachable from the webapp).

Every function here is best-effort and never raises — a classification that
fails (model down, busy) is simply left for the next nightly pass.
"""
from __future__ import annotations

import asyncio

from . import photos, tryon, wardrobe


async def refresh_garment(garment_id: int, user_id: int) -> dict:
    """Describe a garment once (vision) and store type+description. Returns
    {'type': ..., 'description': ...} or {} on failure (never raises)."""
    try:
        w = wardrobe.Wardrobe()
        g = w.get(user_id, garment_id)
        if g is None:
            return {}
        info = await tryon.describe_garment(tryon._load_garment_bytes(g, user_id))
        tryon.set_garment_vision(garment_id, info.get("type", ""), info.get("description", ""))
        return {"type": info.get("type", ""), "description": info.get("description", "")}
    except Exception:  # noqa: BLE001 — best-effort, retry on next nightly pass
        return {}


async def refresh_photo(photo_id: int, user_id: int) -> dict:
    """Classify a base photo once (deterministic mask+skin check) and store the
    person-wearing type. Returns {'type': ...} or {} on failure."""
    try:
        data = photos.photo_bytes(user_id, photo_id)
        style = await tryon.classify_person_style(data)
        tryon.set_photo_vision(photo_id, style)
        return {"type": style}
    except Exception:  # noqa: BLE001 — best-effort, retry on next nightly pass
        return {}


def refresh_garment_sync(garment_id: int, user_id: int) -> dict:
    """Sync wrapper (for scripts / non-async callers)."""
    try:
        return asyncio.run(refresh_garment(garment_id, user_id))
    except Exception:  # noqa: BLE001
        return {}


def refresh_photo_sync(photo_id: int, user_id: int) -> dict:
    """Sync wrapper (for scripts / non-async callers)."""
    try:
        return asyncio.run(refresh_photo(photo_id, user_id))
    except Exception:  # noqa: BLE001
        return {}
