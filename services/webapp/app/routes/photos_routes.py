"""Photos endpoints — person/base photos used as try-on sources."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import photopick, photos
from ..deps import get_current_user
from ..media import garment_image_path
from ..store import wardrobe

router = APIRouter()


class PhotoDescriptionRequest(BaseModel):
    description: str = Field("", max_length=200)


@router.get("/api/photos")
def list_photos(user: dict = Depends(get_current_user)) -> list[dict]:
    return photos.list(user["id"])


@router.get("/api/photos/best-for-garment/{garment_id}")
def best_photo_for_garment(
    garment_id: int, fast: bool = False, user: dict = Depends(get_current_user)
) -> dict:
    """Pick the best saved person photo as the try-on base for a SPECIFIC
    garment. Driven by OUTFIT MATCH (vision LLM: a swimsuit garment wants a
    swimsuit-ish photo, a dress wants a dress photo, etc.) with a pure-PIL
    fallback when the vision model is down or fast=true is passed. Returns best
    + full ranked list (best-first), each entry carrying score/grade/reason/method."""
    g = wardrobe.get(user["id"], garment_id)
    if g is None:
        raise HTTPException(404, f"garment {garment_id} not found in your wardrobe")
    path = garment_image_path(user["id"], garment_id)
    if path is None:
        raise HTTPException(404, "no image for this garment")
    ranked = photopick.rank_photos_for_garment(user["id"], path.read_bytes(), g.category, fast=fast)
    best = ranked[0] if ranked else None
    return {
        "garment_id": garment_id,
        "garment_name": g.name,
        "method": best["method"] if best else "none",
        "best_id": best["id"] if best else None,
        "best": best,
        "ranked": ranked,
    }


@router.post("/api/photos")
async def upload_photo(
    person: UploadFile = File(...), user: dict = Depends(get_current_user)
) -> dict:
    data = await person.read()
    if not data:
        raise HTTPException(400, "empty image")
    ext = Path(person.filename or "").suffix
    try:
        return photos.upload(user["id"], data, ext)
    except photos.PhotoError as ex:
        raise HTTPException(400, str(ex)) from ex


@router.post("/api/photos/{photo_id}/default")
def set_default_photo(photo_id: int, user: dict = Depends(get_current_user)) -> dict:
    try:
        photos.set_default(user["id"], photo_id)
    except photos.PhotoError as ex:
        raise HTTPException(404, str(ex)) from ex
    return {"ok": True}


@router.patch("/api/photos/{photo_id}")
def update_photo(
    photo_id: int, req: PhotoDescriptionRequest, user: dict = Depends(get_current_user)
) -> dict:
    try:
        return photos.set_description(user["id"], photo_id, req.description)
    except photos.PhotoError as ex:
        raise HTTPException(404, str(ex)) from ex


@router.delete("/api/photos/{photo_id}")
def delete_photo(photo_id: int, user: dict = Depends(get_current_user)) -> dict:
    try:
        photos.delete(user["id"], photo_id)
    except photos.PhotoError as ex:
        raise HTTPException(404, str(ex)) from ex
    return {"ok": True}


@router.get("/api/photos/{photo_id}/image")
def photo_image(photo_id: int, user: dict = Depends(get_current_user)) -> FileResponse:
    try:
        path = photos.photo_path(user["id"], photo_id)
    except photos.PhotoError as ex:
        raise HTTPException(404, str(ex)) from ex
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})
