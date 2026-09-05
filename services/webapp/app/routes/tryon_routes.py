"""Try-on endpoints — single garment, chained outfit, clip (SVD), result serving."""
from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from .. import editor, embeddings, interactions, photopick, photos, svd, tryon
from ..deps import get_current_user
from ..media import (
    IMAGE_CACHE_CONTROL,
    UPLOADS_CACHE_CONTROL,
    UPLOAD_DIR,
    garment_image_path,
    image_variant,
    media_type_for,
)
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
        # Single canonical pipeline (2026-09-02): IDM-VTON, same as outfits.
        result = await tryon.run_tryon_model("idm_vton", person_bytes, garment, user["id"])
    except tryon.ComfyUnavailable as ex:
        raise HTTPException(503, str(ex)) from ex
    out_dir = UPLOAD_DIR / str(user["id"]) / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_name = f"tryon_{garment_id}_{int(time.time())}.png"
    (out_dir / out_name).write_bytes(result)
    return {"result_url": f"/api/uploads/{out_name}", "garment_id": garment_id}


async def _pick_person_photo(user: dict, garments: list) -> int | None:
    """Pick the saved photo that best SUITS the look — look at what each base
    photo shows vs the clothes being tried on and choose the best match. A
    bottom look scores highest on a base already wearing the same kind of
    bottom (shorts → a bare-leg/shorts base, pants → a separates base); a
    dress base never suits a bottom look. Returns the best-suited photo id, or
    None when no saved photo is a workable match (the caller must NOT render
    then).

    Garment type + base-photo type come from the STORED vision cache (computed
    at upload / nightly batch) — no live vision, no ComfyUI mask pass at pick
    time. Ranking uses the existing photopick quality signal; if photo
    embeddings are present they're blended in as an extra similarity term."""
    target = None
    for g in garments:
        if tryon.CLOTH_TYPE.get(g.category, "upper") == "lower":
            target = g
            break
    if target is None:
        target = garments[0]
    path = garment_image_path(user["id"], target.id)
    if path is None:
        return None
    want = await tryon.garment_base_type(target, user["id"])
    ranked = photopick.rank_photos_for_garment(
        user["id"], path.read_bytes(), target.category
    )
    if not ranked:
        return None
    if want is None:
        return ranked[0]["id"]  # no bottom in the look → best-quality base is fine
    # garment↔photo embedding similarity (FashionCLIP) — 0.0 when not embedded yet
    emb = embeddings.get_vector(target.id) if _has_emb(target) else None
    photo_embs = embeddings.all_photo_vectors()
    best_id, best_score = None, -10**9
    for row in ranked[:8]:
        try:
            data = photos.photo_bytes(user["id"], row["id"])
        except photos.PhotoError:
            continue
        style = await tryon.photo_style_cached(data, row["id"])
        base = row.get("score") or 50  # photopick quality/vision signal
        # how well THIS base suits the garment being tried on (garment type is
        # decided by LOOKING at the garment image, never its name)
        if want == "shorts":
            match = 40 if style == "shorts" else (-25 if style == "pants" else -70)
        elif want == "dress":
            match = 40 if style == "dress" else -70  # dress needs a dress base
        else:  # pants/skirt look
            match = 25 if style in ("pants", "shorts") else -70
        sim = 0.0
        if emb is not None and row["id"] in photo_embs:
            sim = embeddings.cosine(emb, photo_embs[row["id"]])
        score = base + match + 15 * sim
        if score > best_score:
            best_id, best_score = row["id"], score
    return best_id


def _has_emb(target) -> bool:
    """True when the garment has a stored FashionCLIP vector (so we can blend
    embedding similarity into base ranking)."""
    try:
        return embeddings.get_vector(target.id) is not None
    except Exception:  # noqa: BLE001
        return False


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

    SINGLE canonical pipeline (2026-09-02): there is exactly ONE image-
    generation path — the current IDM-VTON workflow (CatVTON owns geometry,
    IDM only re-textures inside CatVTON's masks; face + background are
    preserved pixel-identical). No model selection. `prompt` is carried through
    the response for promptable edit models (the edit endpoint handles it).

    Any render produced from a look (non-empty garment_ids) is auto-saved to
    the Outfits page — one new saved outfit per render (no dedupe: re-rendering
    a look creates a fresh card so it's always obvious the render was saved)."""
    try:
        ids = [int(x) for x in json.loads(garment_ids)]
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(400, "garment_ids must be a JSON array of ids") from ex
    if not ids and not (base_result or photo_id or person):
        raise HTTPException(400, "no garments selected (provide a look, or a base image to re-render)")
    # Resolve the garments ONCE — they must be in the viewer's REAL wardrobe
    # (hard requirement: a render must show real clothes) — and use them to
    # auto-pick the best source person photo for the look.
    garments = []
    if ids:
        for gid in ids:
            g = wardrobe.get_visible(user["id"], gid)
            if g is None:
                raise HTTPException(404, f"garment {gid} not found in your wardrobe")
            garments.append(g)
    # Best-source-photo: the base must MATCH what the look needs (decided by
    # looking at the garments — shorts→bare-leg base, pants→separates, dress→
    # dress base, never pants-for-a-dress). Auto-pick a matching photo unless
    # the caller uploaded a raw image / re-renders an existing render.
    _auto_pick_target = None
    if ids and (not base_result) and person is None and photo_id is None:
        for _g in garments:
            _t = await tryon.garment_base_type(_g, user["id"])
            if _t is not None:
                _auto_pick_target = _g
                break
    if _auto_pick_target is not None:
        picked = await _pick_person_photo(user, garments)
        if picked is None:
            raise HTTPException(
                400,
                "No saved photo suits this look — a "
                f"{_auto_pick_target.name} look needs a matching base "
                "(shorts→shorts/bare-leg, pants→separates, dress→dress). Add a "
                "suitable photo or pick the base manually.",
            )
        photo_id = picked
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

    # HARD GATE — the base must match the look's garment, or we do NOT run.
    # What the garment needs ('shorts'/'pants'/'dress') is decided by LOOKING at
    # the garment image (vision), never by its name. A shorts look needs a
    # shorts/bare-leg base, a pants look a separates base, a DRESS look a dress
    # base (a pants base makes a dress render as pants — the O-G2KK3X failure).
    # Pick the right image or stop (no pointless renders on the wrong base).
    if ids and (not base_result) and person is None and photo_id is not None:
        _target = next(
            (g for g in garments
             if tryon.CLOTH_TYPE.get(g.category, "upper") == "lower"),
            None,
        )
        if _target is None and len(garments) == 1:
            _target = garments[0]  # single-garment look (e.g. a dress)
        _want = await tryon.garment_base_type(_target, user["id"]) if _target else None
        if _want is not None:
            _style = await tryon.photo_style_cached(person_bytes, photo_id)
            _ok = False
            if _want == "shorts":
                _ok = _style == "shorts"
            elif _want == "pants":
                _ok = _style in ("pants", "shorts")
            else:  # dress
                _ok = _style == "dress"
            if not _ok:
                _need = {
                    "shorts": "a shorts/bare-leg",
                    "pants": "a separates",
                    "dress": "a dress",
                }[_want]
                raise HTTPException(
                    400,
                    f"The selected base doesn't match this {_want} look — {_need} "
                    f"base is needed for {_target.name if _target else 'these clothes'}. "
                    "Pick a matching base photo or let the app auto-pick one.",
                )

    # Record WHICH source person photo produced this render (metadata only —
    # no copies of the image are stored; the base photo stays in place as
    # context for follow-ups).
    person_photo_id = int(photo_id) if photo_id is not None else 0
    person_url = f"/api/photos/{person_photo_id}/image" if person_photo_id else ""
    # SINGLE canonical pipeline (2026-09-02): exactly ONE image-generation
    # path — the current IDM-VTON workflow. No model selection. With an empty
    # look (Saved-image / chat refine mode) the base image passes through
    # untouched — no garments are re-added to an already-rendered image.
    outfit_id: int | None = None
    result_url = ""
    if ids:
        for g in garments:
            interactions.log(user["id"], g.id, "tried_on", {"mode": "outfit"})
        # SINGLE canonical pipeline: IDM-VTON (CatVTON owns geometry, IDM only
        # re-textures inside CatVTON's masks). No model selection.
        try:
            mbytes = await tryon.run_tryon_outfit_model(
                "idm_vton", person_bytes, garments, user["id"]
            )
        except tryon.ComfyUnavailable as ex:
            raise HTTPException(503, str(ex)) from ex
        out_dir = UPLOAD_DIR / str(user["id"]) / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_name = f"tryon_outfit_{int(time.time())}.png"
        (out_dir / out_name).write_bytes(mbytes)
        result_url = f"/api/uploads/{out_name}"
        # every look render auto-saves to the Outfits page
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
        "result_url": result_url,
        "results": [{"model": "idm_vton", "label": "IDM-VTON", "result_url": result_url}]
        if result_url else [],
        "garment_ids": ids,
        "prompt": prompt or "", "outfit_id": outfit_id,
        "person_photo_id": person_photo_id, "person_url": person_url,
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
def get_result(filename: str, size: str = "full",
               user: dict = Depends(get_current_user)) -> FileResponse:
    """Serve a try-on result (or SVD webp clip) only to the user who owns it
    (path-traversal safe). size=thumb/detail serve lazy WebP variants."""
    safe = Path(filename).name  # strips any directory components
    path = UPLOAD_DIR / str(user["id"]) / "out" / safe
    if not path.is_file():
        raise HTTPException(404, "not found")
    if size in ("thumb", "detail"):
        path = image_variant(path, size)
    return FileResponse(path, media_type=media_type_for(path),
                        headers={"Cache-Control": UPLOADS_CACHE_CONTROL})


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
