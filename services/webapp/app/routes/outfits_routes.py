"""Saved-outfit endpoints — list, save, update (name/rating), delete."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..deps import get_current_user
from ..media import garment_dict
from ..store import outfits, wardrobe

router = APIRouter()


class OutfitSave(BaseModel):
    name: str = Field("", max_length=120)
    garment_ids: list[int] = Field(..., min_length=1, max_length=8)
    result_url: str = Field("", max_length=200)


class OutfitUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    rating: int | None = Field(None, ge=0, le=10)


@router.get("/api/outfits")
def list_outfits(user: dict = Depends(get_current_user)) -> list[dict]:
    out = []
    for o in outfits.list(user["id"]):
        d = dict(o)
        gs = []
        for gid in o["garment_ids"]:
            g = wardrobe.get(user["id"], gid)
            if g:
                gs.append(garment_dict(user["id"], g))
        d["garments"] = gs
        out.append(d)
    return out


@router.post("/api/outfits")
def save_outfit(req: OutfitSave, user: dict = Depends(get_current_user)) -> dict:
    for gid in req.garment_ids:
        if wardrobe.get(user["id"], gid) is None:
            raise HTTPException(404, f"garment {gid} not in your wardrobe")
    name = (req.name or "").strip()[:120] or "Saved outfit"
    result_url = (req.result_url or "").strip()[:200]
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
    if fields:
        outfits.update(user["id"], outfit_id, **fields)
    return outfits.get(user["id"], outfit_id)


@router.delete("/api/outfits/{outfit_id}")
def delete_outfit(outfit_id: int, user: dict = Depends(get_current_user)) -> dict:
    if not outfits.delete(user["id"], outfit_id):
        raise HTTPException(404, "outfit not found")
    return {"ok": True}
