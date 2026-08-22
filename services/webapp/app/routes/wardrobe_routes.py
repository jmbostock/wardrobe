"""Wardrobe endpoints — garment CRUD, images, and product-link parsing."""
from __future__ import annotations

from urllib.parse import urljoin

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import imglink, render
from ..deps import get_current_user
from ..media import (
    COLOR_HEX,
    WARDROBE_CATEGORIES,
    fetch_product_image,
    fetch_url_bytes,
    garment_dict,
    garment_image_path,
    save_garment_image,
    validate_image,
)
from ..store import outfits, wardrobe

router = APIRouter()


class WardrobeCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    category: str = Field(...)
    color: str = Field("", max_length=60)
    owned: bool = True  # False = "to buy" / wishlist item
    image_url: str | None = Field(None, max_length=2000)


class WardrobeUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    category: str | None = Field(None)
    color: str | None = Field(None, max_length=40)
    rating: int | None = Field(None, ge=0, le=10)
    owned: bool | None = Field(None)


class ImageUrlRequest(BaseModel):
    url: str = Field(..., max_length=2000)


class ParseLinkRequest(BaseModel):
    url: str = Field(..., max_length=2000)


@router.get("/api/wardrobe")
def list_wardrobe(user: dict = Depends(get_current_user)) -> list[dict]:
    items = [garment_dict(user["id"], g) for g in wardrobe.all(user["id"])]
    # used_count = how many saved outfits reference this garment (for "most used" sort)
    counts: dict[int, int] = {}
    for o in outfits.list(user["id"]):
        for gid in o["garment_ids"]:
            counts[gid] = counts.get(gid, 0) + 1
    for d in items:
        d["used_count"] = counts.get(d["id"], 0)
    return items


@router.post("/api/wardrobe")
def create_garment(req: WardrobeCreate, user: dict = Depends(get_current_user)) -> dict:
    category = req.category.strip().lower()
    if category not in WARDROBE_CATEGORIES:
        raise HTTPException(
            400, f"category must be one of: {', '.join(sorted(WARDROBE_CATEGORIES))}"
        )
    name = req.name.strip()[:200]
    if not name:
        raise HTTPException(400, "name required")
    color = req.color.strip().lower()
    color_hex = COLOR_HEX.get(color, "#8a8f98")
    g = wardrobe.create(
        user["id"], name, category, color_hex=color_hex, color_tags=color,
        owned=req.owned,
    )
    if req.image_url:
        data = fetch_product_image(req.image_url)
        ext = validate_image(data)
        save_garment_image(user["id"], g.id, data, ext)
    return garment_dict(user["id"], g)


@router.post("/api/wardrobe/parse-link")
def parse_garment_link(req: ParseLinkRequest, user: dict = Depends(get_current_user)) -> dict:
    """Inspect a store page (or direct image URL) and return the product name/
    color/category plus a gallery of candidate images for the UI to pick one."""
    data = fetch_url_bytes(req.url)
    if imglink.is_image_bytes(data):
        return {"name": "", "description": "", "color": "", "category": None, "images": [req.url]}
    info = imglink.extract_product_page(data.decode("utf-8", errors="ignore"))
    if not info["images"]:
        # JS-rendered page (SPA like Express): the product HTML only exists after
        # client-side render — try headless Chromium before giving up.
        rendered = render.render_page_html(req.url)
        if rendered:
            info = imglink.extract_product_page(rendered)
    if not info["images"]:
        raise HTTPException(
            400,
            "no product images found on that page — try a direct image URL or upload the file",
        )
    # resolve protocol-relative / relative image URLs against the page URL,
    # then clean them (drop tracking params, bump width) so the picker shows
    # high-res previews and the chosen URL fetches clean.
    info["images"] = [imglink.clean_image_url(urljoin(req.url, u)) for u in info["images"]]
    return info


@router.post("/api/wardrobe/{garment_id}/image")
async def upload_garment_image(
    garment_id: int,
    image: UploadFile = File(...),
    user: dict = Depends(get_current_user),
) -> dict:
    g = wardrobe.get(user["id"], garment_id)
    if g is None:
        raise HTTPException(404, "garment not found")
    data = await image.read()
    ext = validate_image(data)
    save_garment_image(user["id"], garment_id, data, ext)
    return garment_dict(user["id"], g)


@router.post("/api/wardrobe/{garment_id}/image-url")
def garment_image_from_url(
    garment_id: int, req: ImageUrlRequest, user: dict = Depends(get_current_user)
) -> dict:
    g = wardrobe.get(user["id"], garment_id)
    if g is None:
        raise HTTPException(404, "garment not found")
    data = fetch_product_image(req.url)
    ext = validate_image(data)
    save_garment_image(user["id"], garment_id, data, ext)
    return garment_dict(user["id"], g)


@router.get("/api/wardrobe/{garment_id}/image")
def garment_image(garment_id: int, user: dict = Depends(get_current_user)) -> FileResponse:
    g = wardrobe.get(user["id"], garment_id)
    if g is None:
        raise HTTPException(404, "garment not found")
    path = garment_image_path(user["id"], garment_id)
    if path is None:
        raise HTTPException(404, "no image for this garment")
    media = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".gif": "image/gif",
    }.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media)


@router.patch("/api/wardrobe/{garment_id}")
def update_garment(
    garment_id: int, req: WardrobeUpdate, user: dict = Depends(get_current_user)
) -> dict:
    """Edit a garment's name / category / color / rating. Any field may be omitted."""
    g = wardrobe.get(user["id"], garment_id)
    if g is None:
        raise HTTPException(404, "garment not found")
    name = (req.name if req.name is not None else g.name).strip()
    if not name:
        raise HTTPException(400, "name required")
    category = (req.category if req.category is not None else g.category).strip().lower()
    if category not in WARDROBE_CATEGORIES:
        raise HTTPException(
            400, f"category must be one of: {', '.join(sorted(WARDROBE_CATEGORIES))}"
        )
    color = (
        req.color if req.color is not None else (g.color_tags or "").split(",")[0]
    ).strip().lower()
    color_hex = COLOR_HEX.get(color, "#8a8f98")
    fields = {
        "name": name, "category": category, "color_hex": color_hex, "color_tags": color,
    }
    if req.rating is not None:
        fields["rating"] = req.rating
    if req.owned is not None:
        fields["owned"] = 1 if req.owned else 0
    wardrobe.update(user["id"], garment_id, **fields)
    return garment_dict(user["id"], wardrobe.get(user["id"], garment_id))


@router.delete("/api/wardrobe/{garment_id}")
def delete_garment(garment_id: int, user: dict = Depends(get_current_user)) -> dict:
    g = wardrobe.get(user["id"], garment_id)
    if g is None:
        raise HTTPException(404, "garment not found")
    path = garment_image_path(user["id"], garment_id)
    if path:
        try:
            path.unlink()
        except OSError:
            pass
    wardrobe.delete(user["id"], garment_id)
    return {"ok": True}
