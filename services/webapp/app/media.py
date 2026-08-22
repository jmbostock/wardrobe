"""Shared garment-image helpers used by several route modules.

Centralises wardrobe image locations, URL fetching, product-page extraction
and the public garment dict so routers stay thin and consistent.
"""
from __future__ import annotations

import io
from pathlib import Path
from urllib.parse import urljoin

import httpx
from fastapi import HTTPException
from PIL import Image, ImageOps

from . import phash, render
from .config import settings
from .imglink import (
    clean_image_url,
    detect_ext,
    extract_image_url_from_html,
    is_image_bytes,
)
from .store import wardrobe

# iPhone photos come in as HEIC — pillow-heif adds a PIL opener so we can
# decode (and normalize to JPEG on save) without any system libheif.
try:
    import pillow_heif

    pillow_heif.register_heif_opener()
except Exception:  # noqa: BLE001 — optional dep; HEIC just won't be readable
    pass

WARDROBE_DIR = Path(settings.data_dir) / "wardrobe"
UPLOAD_DIR = Path(settings.data_dir) / "uploads"

WARDROBE_CATEGORIES = {"top", "bottom", "dress", "outerwear", "footwear", "accessory"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".heic"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024
COLOR_HEX = {
    "white": "#f2f2f2", "black": "#1a1a1a", "gray": "#8a8f98", "grey": "#8a8f98",
    "navy": "#1f2a44", "blue": "#3b5ba8", "red": "#a33333", "green": "#2e4a3a",
    "beige": "#d9c9a3", "brown": "#6b4a2f", "tan": "#c8b98a", "pink": "#d9b3a0",
    "burgundy": "#6d2332", "purple": "#5b3a6d", "yellow": "#d9c04a", "orange": "#c96a2e",
    "teal": "#2c4f46", "cream": "#f2efe6", "khaki": "#c8b98a", "olive": "#6b7a3a",
}


def garment_image_path(user_id: int, garment_id: int) -> Path | None:
    """Find the on-disk image for a garment (any supported extension)."""
    d = WARDROBE_DIR / str(user_id)
    if not d.is_dir():
        return None
    for p in sorted(d.glob(f"{garment_id}.*")):
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
            return p
    return None


def validate_image(data: bytes) -> str:
    if not data:
        raise HTTPException(400, "empty image")
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(400, "image too large (>10MB)")
    if not is_image_bytes(data):
        raise HTTPException(400, "not a recognizable image (PNG/JPG/GIF/WebP)")
    return detect_ext(data)


def _heic_to_jpeg(data: bytes) -> bytes:
    """Normalize a HEIC photo to JPEG so downstream (try-on, serving) is uniform."""
    try:
        import pillow_heif  # noqa: F401 — triggers opener registration if missing
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "HEIC photos need pillow-heif (rebuild webapp)")
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=90)
    return buf.getvalue()


def save_garment_image(user_id: int, garment_id: int, data: bytes, ext: str) -> Path:
    d = WARDROBE_DIR / str(user_id)
    d.mkdir(parents=True, exist_ok=True)
    for old in d.glob(f"{garment_id}.*"):
        try:
            old.unlink()
        except OSError:
            pass
    if ext == "heic":  # normalize iPhone photos to JPEG on ingest
        data = _heic_to_jpeg(data)
        ext = "jpg"
    path = d / f"{garment_id}.{ext}"
    path.write_bytes(data)
    # record image + perceptual hash for near-duplicate detection
    wardrobe.update(user_id, garment_id, image_path=path.name, phash=phash.image_phash(data))
    return path


def near_duplicates(
    user_id: int,
    phash_hex: str,
    exclude_id: int | None = None,
    threshold: int = phash.SIMILAR_THRESHOLD,
) -> list[dict]:
    """Existing garments for this user whose dHash is within `threshold` bits of
    the given hash — i.e. likely the same (or nearly the same) item. Sorted by
    closest first. Empty list = no near-duplicate."""
    if not phash_hex:
        return []
    out: list[dict] = []
    for g in wardrobe.all(user_id):
        if not g.phash or g.id == exclude_id:
            continue
        d = phash.hamming(phash_hex, g.phash)
        if d <= threshold:
            out.append({"id": g.id, "name": g.name, "distance": d})
    out.sort(key=lambda x: x["distance"])
    return out


def nearest_dup(
    user_id: int, phash_hex: str, exclude_id: int | None = None,
    threshold: int = phash.SIMILAR_THRESHOLD,
) -> dict | None:
    """Closest match only (for the wardrobe grid's "similar to X" note)."""
    dups = near_duplicates(user_id, phash_hex, exclude_id=exclude_id, threshold=threshold)
    return dups[0] if dups else None


def fetch_url_bytes(url: str) -> bytes:
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "URL must start with http:// or https://")
    try:
        r = httpx.get(url, timeout=20, follow_redirects=True)
    except Exception as ex:  # noqa: BLE001 — surface fetch failures clearly
        raise HTTPException(400, f"could not fetch URL: {ex}") from ex
    if r.status_code != 200:
        raise HTTPException(400, f"URL returned HTTP {r.status_code}")
    if not r.content:
        raise HTTPException(400, "URL returned an empty body")
    if len(r.content) > MAX_IMAGE_BYTES:
        raise HTTPException(400, "image too large (>10MB)")
    return r.content


def fetch_product_image(url: str) -> bytes:
    """Fetch an image from a direct image URL OR a store product page (HTML).
    For HTML pages we extract the product image (og:image → JSON-LD → largest
    product <img>) and fetch that. Protocol-relative / relative image URLs are
    resolved against the page URL. URLs are cleaned first (junk query params
    dropped, width/wid/w bumped up) so we don't save 92px CDN thumbnails. If the
    page is a JS-rendered SPA with no images in the raw HTML, we render it in
    headless Chromium as a fallback before giving up."""
    data = fetch_url_bytes(clean_image_url(url))
    if is_image_bytes(data):
        return data
    html = data.decode("utf-8", errors="ignore")
    img_url = extract_image_url_from_html(html)
    if not img_url:
        rendered = render.render_page_html(clean_image_url(url))
        if rendered:
            img_url = extract_image_url_from_html(rendered)
    if not img_url:
        raise HTTPException(
            400,
            "no product image found on that page — try a direct image URL or upload the file",
        )
    return fetch_url_bytes(clean_image_url(urljoin(url, img_url)))


def garment_dict(user_id: int, g) -> dict:
    d = g.to_dict()
    d["has_image"] = garment_image_path(user_id, g.id) is not None
    # nearest existing garment this one is a near-duplicate of (for "similar to
    # X" notes) — computed only when we have a phash
    nd = nearest_dup(user_id, g.phash, exclude_id=g.id) if g.phash else None
    d["near_dup_of"] = nd
    return d
