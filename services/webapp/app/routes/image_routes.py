"""Image-quality scoring endpoint (pure-PIL heuristics, no ML)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from .. import imageqa, photos
from ..deps import get_current_user
from ..media import garment_image_path
from ..store import wardrobe

router = APIRouter()


@router.post("/api/image-quality")
async def image_quality(
    kind: str = Form(...),
    image: UploadFile | None = File(None),
    photo_id: int | None = Form(None),
    garment_id: int | None = Form(None),
    user: dict = Depends(get_current_user),
) -> dict:
    """Score a person photo or garment image before try-on and explain what
    would make it better. Returns {score, grade, issues, tips}."""
    if kind == "person":
        if photo_id is not None:
            try:
                data = photos.photo_bytes(user["id"], photo_id)
            except photos.PhotoError as ex:
                raise HTTPException(404, str(ex)) from ex
        elif image is not None:
            data = await image.read()
        else:
            raise HTTPException(400, "provide image or photo_id")
        if not data:
            raise HTTPException(400, "empty image")
        return imageqa.assess_person(data)
    if kind == "garment":
        if garment_id is None:
            raise HTTPException(400, "garment_id required")
        g = wardrobe.get(user["id"], garment_id)
        if g is None:
            raise HTTPException(404, "garment not found")
        path = garment_image_path(user["id"], garment_id)
        if path is None:
            raise HTTPException(404, "no image for this garment")
        return imageqa.assess_garment(path.read_bytes())
    raise HTTPException(400, "kind must be 'person' or 'garment'")
