"""Try-on endpoints — single garment, chained outfit, clip (SVD), result serving."""
from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from .. import editor, interactions, photos, svd, tryon
from ..deps import get_current_user
from ..media import UPLOAD_DIR
from ..store import clips, outfits, wardrobe

router = APIRouter()


@router.post("/api/tryon")
async def do_tryon(
    garment_id: int = Form(...),
    person: UploadFile | None = File(None),
    photo_id: int | None = Form(None),
    user: dict = Depends(get_current_user),
) -> dict:
    garment = wardrobe.get_visible(user["id"], garment_id)
    if garment is None:
        raise HTTPException(404, f"garment {garment_id} not found in your wardrobe")
    interactions.log(user["id"], garment_id, "tried_on", {"mode": "single"})
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
    outfit_name: str | None = Form(None),
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
    upgrade path).

    Any render produced from a look (non-empty garment_ids) is auto-saved to
    the Outfits page — one new saved outfit per render (no dedupe: re-rendering
    a look creates a fresh card so it's always obvious the render was saved)."""
    try:
        ids = [int(x) for x in json.loads(garment_ids)]
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(400, "garment_ids must be a JSON array of ids") from ex
    if not ids and not (base_result or photo_id or person):
        raise HTTPException(400, "no garments selected (provide a look, or a base image to re-render)")
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
    # Record WHICH source person photo produced this render (metadata only —
    # no copies of the image are stored; the base photo stays in place as
    # context for follow-ups).
    person_photo_id = int(photo_id) if photo_id is not None else 0
    person_url = f"/api/photos/{person_photo_id}/image" if person_photo_id else ""
    # Apply the look (garments) if any. With an empty look (Saved-image / chat
    # refine mode) the base image passes through untouched — no garments are
    # re-added to an already-rendered image. A promptable model (Phase 5) can
    # later use `prompt` to actually alter the image here.
    outfit_id: int | None = None
    if ids:
        try:
            for gid in ids:
                garment = wardrobe.get_visible(user["id"], gid)
                if garment is None:
                    raise HTTPException(404, f"garment {gid} not found in your wardrobe")
                interactions.log(user["id"], gid, "tried_on", {"mode": "outfit"})
                person_bytes = await tryon.run_tryon(person_bytes, garment, user["id"])
        except tryon.ComfyUnavailable as ex:
            raise HTTPException(503, str(ex)) from ex
        out_dir = UPLOAD_DIR / str(user["id"]) / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_name = f"tryon_outfit_{int(time.time())}.png"
        (out_dir / out_name).write_bytes(person_bytes)
        result_url = f"/api/uploads/{out_name}"
        outfit_id = _auto_save_outfit(
            user["id"], ids, result_url, outfit_name or "",
            person_photo_id=person_photo_id, person_url=person_url,
        )
    elif base_result:
        # garment-free refine of an existing render — nothing new to draw, so
        # return the same image without writing a duplicate file.
        result_url = base_result
    else:
        # first saved-image refine from a person photo: serve the photo as a
        # stable result so the UI can compare base vs result (one file only).
        out_dir = UPLOAD_DIR / str(user["id"]) / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_name = f"tryon_refine_{int(time.time())}.png"
        (out_dir / out_name).write_bytes(person_bytes)
        result_url = f"/api/uploads/{out_name}"
    return {
        "result_url": result_url, "garment_ids": ids, "prompt": prompt or "",
        "outfit_id": outfit_id, "person_photo_id": person_photo_id, "person_url": person_url,
    }


def _auto_save_outfit(
    user_id: int, ids: list[int], result_url: str, name: str,
    person_photo_id: int = 0, person_url: str = "",
) -> int:
    """Save a rendered look to the Outfits page. Every render creates a NEW
    outfit row (no dedupe). Stores metadata about the source person photo
    (person_photo_id + a reference URL) — never a copy of the image itself."""
    final_name = (name or "").strip()[:120] or ("Outfit " + time.strftime("%b %d"))
    return outfits.create(
        user_id, final_name, ids, result_url=result_url,
        person_photo_id=person_photo_id, person_url=person_url,
    )["id"]


@router.get("/api/uploads/{filename}")
def get_result(filename: str, user: dict = Depends(get_current_user)) -> FileResponse:
    """Serve a try-on result (or SVD webp clip) only to the user who owns it
    (path-traversal safe)."""
    safe = Path(filename).name  # strips any directory components
    path = UPLOAD_DIR / str(user["id"]) / "out" / safe
    if not path.is_file():
        raise HTTPException(404, "not found")
    media = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".gif": "image/gif",
    }
    return FileResponse(path, media_type=media.get(path.suffix.lower(), "application/octet-stream"))


@router.post("/api/tryon/edit")
async def do_tryon_edit(
    prompt: str = Form(...),
    base_result: str | None = Form(None),
    image: UploadFile | None = File(None),
    user: dict = Depends(get_current_user),
) -> dict:
    """Edit a render via the chat bar (InstructPix2Pix engine).

    `base_result` is an owner-only render to edit (from a prior try-on / saved
    outfit). Alternatively pass `image` directly. Returns the edited render."""
    if base_result:
        safe = Path(base_result).name  # strips any directory components
        path = UPLOAD_DIR / str(user["id"]) / "out" / safe
        if not path.is_file():
            raise HTTPException(404, "base result not found")
        base_bytes = path.read_bytes()
    elif image is not None:
        base_bytes = await image.read()
    else:
        raise HTTPException(400, "provide base_result or an image")
    if not base_bytes:
        raise HTTPException(400, "empty image")
    prompt = (prompt or "").strip()[:300]
    if not prompt:
        raise HTTPException(400, "prompt required")
    try:
        result = await editor.run_edit(base_bytes, prompt)
    except tryon.ComfyUnavailable as ex:
        raise HTTPException(503, str(ex)) from ex
    out_dir = UPLOAD_DIR / str(user["id"]) / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_name = f"edit_{int(time.time())}.png"
    (out_dir / out_name).write_bytes(result)
    return {"result_url": f"/api/uploads/{out_name}", "prompt": prompt}


@router.post("/api/tryon/clip")
async def do_tryon_clip(
    base_result: str = Form(...),
    outfit_id: int | None = Form(None),
    user: dict = Depends(get_current_user),
) -> dict:
    """Queue an SVD motion clip for a try-on render. Non-blocking: submits the
    job to ComfyUI (which queues it) and returns {clip_id} immediately. The
    frontend polls GET /api/clips/{clip_id} until status == done."""
    safe = Path(base_result).name  # strips any directory components
    path = UPLOAD_DIR / str(user["id"]) / "out" / safe
    if not path.is_file():
        raise HTTPException(404, "base result not found")
    image_bytes = path.read_bytes()
    try:
        prompt_id = await svd.submit_svd(image_bytes)
    except tryon.ComfyUnavailable as ex:
        raise HTTPException(503, str(ex)) from ex
    clip = clips.create(user["id"], prompt_id, outfit_id=outfit_id or 0)
    return {"clip_id": clip["id"], "status": clip["status"]}


@router.get("/api/clips/by-outfit/{outfit_id}")
def get_clip_for_outfit(outfit_id: int, user: dict = Depends(get_current_user)) -> dict:
    """Latest clip attached to an outfit (any status). Lets the Outfits page
    show/resume an in-progress SVD clip that was started from the Try-on tab
    (or from the outfit's own detail card)."""
    clip = clips.latest_by_outfit(user["id"], outfit_id)
    if clip is None:
        return {"clip_id": None, "status": "none"}
    return {"clip_id": clip["id"], "status": clip["status"],
            "result_url": clip["result_url"], "error": clip["error"]}


@router.get("/api/clips/{clip_id}")
async def get_clip_status(clip_id: int, user: dict = Depends(get_current_user)) -> dict:
    clip = clips.get(user["id"], clip_id)
    if clip is None:
        raise HTTPException(404, "clip not found")
    if clip["status"] in ("done", "error"):
        return {"clip_id": clip_id, "status": clip["status"],
                "result_url": clip["result_url"], "error": clip["error"]}
    try:
        status, data = await svd.check_svd(clip["prompt_id"])
    except tryon.ComfyUnavailable as ex:
        clips.update(user["id"], clip_id, status="error", error=str(ex))
        return {"clip_id": clip_id, "status": "error", "result_url": "", "error": str(ex)}
    if status == "done" and data:
        out_dir = UPLOAD_DIR / str(user["id"]) / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_name = f"clip_{clip_id}_{int(time.time())}.webp"
        (out_dir / out_name).write_bytes(data)
        result_url = f"/api/uploads/{out_name}"
        clips.update(user["id"], clip_id, status="done", result_url=result_url)
        if clip["outfit_id"]:
            outfits.update(user["id"], clip["outfit_id"], motion_url=result_url)
        return {"clip_id": clip_id, "status": "done", "result_url": result_url, "error": ""}
    if status == "running":
        clips.update(user["id"], clip_id, status="running")
    return {"clip_id": clip_id, "status": "running", "result_url": "", "error": ""}
