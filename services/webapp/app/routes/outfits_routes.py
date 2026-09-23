"""Saved-outfit endpoints — list, save, update (name/rating), refine, delete."""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..deps import get_current_user
from .. import interactions, tryon
from ..media import UPLOAD_DIR, garment_dict
from ..store import outfits, wardrobe

router = APIRouter()


class OutfitSave(BaseModel):
    name: str = Field("", max_length=120)
    garment_ids: list[int] = Field(..., min_length=1, max_length=8)
    result_url: str = Field("", max_length=200)


class OutfitUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    rating: int | None = Field(None, ge=0, le=10)


class OutfitRefine(BaseModel):
    prompt: str = Field(..., min_length=2, max_length=300)


@router.get("/api/outfits")
def list_outfits(user: dict = Depends(get_current_user)) -> list[dict]:
    out = []
    for o in outfits.list(user["id"]):
        d = dict(o)
        gs = []
        for gid in o["garment_ids"]:
            g = wardrobe.get_visible(user["id"], gid)  # family-shared items expand too
            if g:
                gs.append(garment_dict(user["id"], g))
        d["garments"] = gs
        out.append(d)
    return out


@router.post("/api/outfits")
def save_outfit(req: OutfitSave, user: dict = Depends(get_current_user)) -> dict:
    for gid in req.garment_ids:
        if wardrobe.get_visible(user["id"], gid) is None:
            raise HTTPException(404, f"garment {gid} not in your wardrobe")
    name = (req.name or "").strip()[:120] or "Saved outfit"
    result_url = (req.result_url or "").strip()[:200]
    interactions.log_many(user["id"], req.garment_ids, "saved", {"name": name})
    return outfits.create(user["id"], name, req.garment_ids, result_url=result_url)


@router.patch("/api/outfits/{outfit_id}")
def update_outfit(
    outfit_id: int, req: OutfitUpdate, user: dict = Depends(get_current_user)
) -> dict:
    """Edit a saved outfit's name and/or rating. Any field may be omitted."""
    o = outfits.get(user["id"], outfit_id)
    if o is None:
        raise HTTPException(404, "outfit not found")
    fields: dict = {}
    if req.name is not None:
        name = req.name.strip()[:120]
        if not name:
            raise HTTPException(400, "name required")
        fields["name"] = name
    if req.rating is not None:
        fields["rating"] = req.rating
        kind = "rated_up" if req.rating >= 7 else ("rated_down" if 1 <= req.rating <= 3 else None)
        if kind:
            interactions.log_many(user["id"], o["garment_ids"], kind, {"rating": req.rating})
    if fields:
        outfits.update(user["id"], outfit_id, **fields)
    return outfits.get(user["id"], outfit_id)


@router.delete("/api/outfits/{outfit_id}")
def delete_outfit(outfit_id: int, user: dict = Depends(get_current_user)) -> dict:
    if not outfits.delete(user["id"], outfit_id):
        raise HTTPException(404, "outfit not found")
    return {"ok": True}


@router.post("/api/outfits/{outfit_id}/refine")
async def refine_outfit(
    outfit_id: int, req: OutfitRefine, user: dict = Depends(get_current_user)
) -> dict:
    """Re-render a saved outfit from a free-text instruction — restyle the
    clothes, change the pose, adjust the light. Runs on Qwen-Image-2.1
    (tryon.refine_render), the same renderer the Try-on page uses.

    NOTHING IS OVERWRITTEN. The original outfit keeps its render and the
    refined version is saved as a NEW outfit carrying the same garments, so the
    two sit side by side on the Outfits page and can be compared or refined
    again. (Standing rule: renders are permanent artifacts — only ever create.)

    Synchronous from the client's point of view: one edit pass takes ~60-120s,
    so the button shows progress and the request just waits."""
    o = outfits.get(user["id"], outfit_id)
    if o is None:
        raise HTTPException(404, "outfit not found")
    if not o.get("result_url"):
        raise HTTPException(400, "this outfit has no render to refine yet")
    # owner-only, path-traversal safe — same rule as the result-serving route
    src = UPLOAD_DIR / str(user["id"]) / "out" / Path(o["result_url"]).name
    if not src.is_file():
        raise HTTPException(404, "the render file for this outfit is missing")
    prompt = (req.prompt or "").strip()[:300]
    if not prompt:
        raise HTTPException(400, "describe the change you want")
    try:
        rendered = await tryon.refine_render(src.read_bytes(), prompt)
    except tryon.ComfyUnavailable as ex:
        raise HTTPException(503, str(ex)) from ex

    out_dir = UPLOAD_DIR / str(user["id"]) / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_name = f"tryon_refine_{outfit_id}_{int(time.time())}.png"
    (out_dir / out_name).write_bytes(rendered)
    result_url = f"/api/uploads/{out_name}"

    fresh = outfits.create(
        user["id"], f"{o['name']} — refined"[:120], list(o["garment_ids"]),
        result_url=result_url,
        person_photo_id=o.get("person_photo_id") or 0,
        person_url=o.get("person_url") or "",
    )
    return {"outfit": fresh, "from_outfit_id": outfit_id,
            "result_url": result_url, "prompt": prompt}
