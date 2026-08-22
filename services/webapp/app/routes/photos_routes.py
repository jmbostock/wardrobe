"""Photos endpoints — person/base photos used as try-on sources."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import photos
from ..deps import get_current_user

router = APIRouter()


class PhotoDescriptionRequest(BaseModel):
    description: str = Field("", max_length=200)


@router.get("/api/photos")
def list_photos(user: dict = Depends(get_current_user)) -> list[dict]:
    return photos.list(user["id"])


@router.get("/api/photos/suitability")
def photo_suitability(
    category: str | None = None, user: dict = Depends(get_current_user)
) -> dict:
    """Rank the user's saved person photos by try-on suitability (best first),
    with an optional garment-category nudge (top/bottom/dress) so the frontend
    can auto-pick the best base photo for a look/swap."""
    ranked = photos.suitability(user["id"], category)
    best = ranked[0] if ranked else None
    return {
        "category": category or "",
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
    return FileResponse(path, media_type="image/jpeg")
