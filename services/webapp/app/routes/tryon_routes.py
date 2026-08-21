"""Try-on endpoints — single garment, chained outfit, and result serving."""
from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from .. import photos, tryon
from ..deps import get_current_user
from ..media import UPLOAD_DIR
from ..store import wardrobe

router = APIRouter()


@router.post("/api/tryon")
async def do_tryon(
    garment_id: int = Form(...),
    person: UploadFile | None = File(None),
    photo_id: int | None = Form(None),
    user: dict = Depends(get_current_user),
) -> dict:
    garment = wardrobe.get(user["id"], garment_id)
    if garment is None:
        raise HTTPException(404, f"garment {garment_id} not found in your wardrobe")
    if photo_id is not None:
        try:
            person_bytes = photos.photo_bytes(user["id"], photo_id)
        except photos.PhotoError as ex:
            raise HTTPException(404, str(ex)) from ex
    elif person is not None:
        person_bytes = await person.read()
    else:
        raise HTTPException(400, "provide a person photo or a saved photo_id")
    if not person_bytes:
        raise HTTPException(400, "empty person image")
    try:
        result = await tryon.run_tryon(person_bytes, garment, user["id"])
    except tryon.ComfyUnavailable as ex:
        raise HTTPException(503, str(ex)) from ex
    out_dir = UPLOAD_DIR / str(user["id"]) / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_name = f"tryon_{garment_id}_{int(time.time())}.png"
    (out_dir / out_name).write_bytes(result)
    return {"result_url": f"/api/uploads/{out_name}", "garment_id": garment_id}


@router.post("/api/tryon/outfit")
async def do_tryon_outfit(
    garment_ids: str = Form(...),
    person: UploadFile | None = File(None),
    photo_id: int | None = Form(None),
    base_result: str | None = Form(None),
    prompt: str | None = Form(None),
    user: dict = Depends(get_current_user),
) -> dict:
    """Try on a whole look: apply each garment in order, chaining the result
    of one onto the next (e.g. top first, then bottom). garment_ids is a JSON
    array of garment ids in apply order.

    The person base can come from three places (in priority order):
      1. `base_result` — a previous try-on render URL (owner-only), used to
         re-render/modify an existing image (the Try-on chat bar sends this).
      2. `photo_id` — a saved person photo.
      3. `person` — an uploaded image.
    `prompt` is the chat instruction (e.g. "make her skinnier"). CatVTON is
    garment-image-only so it isn't used at render time yet, but it's carried
    through the response and ready for promptable models (IDM-VTON / FLUX-Kontext
    upgrade path)."""
    try:
        ids = [int(x) for x in json.loads(garment_ids)]
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(400, "garment_ids must be a JSON array of ids") from ex
    if not ids:
        raise HTTPException(400, "no garments selected")
    if base_result:
        safe = Path(base_result).name  # strips any directory components
        path = UPLOAD_DIR / str(user["id"]) / "out" / safe
        if not path.is_file():
            raise HTTPException(404, "base result not found")
        person_bytes = path.read_bytes()
    elif photo_id is not None:
        try:
            person_bytes = photos.photo_bytes(user["id"], photo_id)
        except photos.PhotoError as ex:
            raise HTTPException(404, str(ex)) from ex
    elif person is not None:
        person_bytes = await person.read()
    else:
        raise HTTPException(400, "provide a person photo, saved photo_id, or base_result")
    if not person_bytes:
        raise HTTPException(400, "empty person image")
    try:
        for gid in ids:
            garment = wardrobe.get(user["id"], gid)
            if garment is None:
                raise HTTPException(404, f"garment {gid} not found in your wardrobe")
            person_bytes = await tryon.run_tryon(person_bytes, garment, user["id"])
    except tryon.ComfyUnavailable as ex:
        raise HTTPException(503, str(ex)) from ex
    out_dir = UPLOAD_DIR / str(user["id"]) / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_name = f"tryon_outfit_{int(time.time())}.png"
    (out_dir / out_name).write_bytes(person_bytes)
    return {"result_url": f"/api/uploads/{out_name}", "garment_ids": ids, "prompt": prompt or ""}


@router.get("/api/uploads/{filename}")
def get_result(filename: str, user: dict = Depends(get_current_user)) -> FileResponse:
    """Serve a try-on result only to the user who owns it (path-traversal safe)."""
    safe = Path(filename).name  # strips any directory components
    path = UPLOAD_DIR / str(user["id"]) / "out" / safe
    if not path.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(path, media_type="image/png")
