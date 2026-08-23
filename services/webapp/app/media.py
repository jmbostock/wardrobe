"""Shared garment-image helpers used by several route modules.

Centralises wardrobe image locations, URL fetching, product-page extraction
and the public garment dict so routers stay thin and consistent.
"""
from __future__ import annotations

import io
import math
import statistics
from pathlib import Path
from urllib.parse import urljoin

import httpx
from fastapi import HTTPException
from PIL import Image, ImageOps

from . import aifill, phash, render
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

WARDROBE_CATEGORIES = {"top", "bottom", "dress", "swimsuit", "outerwear", "footwear", "accessory", "bra"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".heic"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024

# How sizes should be captured/displayed per garment type. `mode` is:
#   list    -> one input, comma list of sizes (datalist suggestions in `options`)
#   wxl     -> pants: Waist (W) x Length (L)  -> stored like "30W x 32L"
#   bandcup -> bra: Band x Cup                 -> stored like "34C"
SIZE_SCHEMAS: dict[str, dict] = {
    "top":       {"mode": "list", "label": "Sizes", "placeholder": "e.g. S, M, L",
                   "options": ["XS", "S", "M", "L", "XL", "XXL", "3XL"]},
    "bottom":    {"mode": "wxl", "label": "Waist × Length",
                   "ph1": "Waist (e.g. 30)", "ph2": "Length (e.g. 32)"},
    "bra":       {"mode": "bandcup", "label": "Band × Cup",
                   "ph1": "Band (e.g. 34)", "ph2": "Cup (e.g. C)"},
    "dress":     {"mode": "list", "label": "Sizes", "placeholder": "e.g. 0,2,4,6 or S,M,L",
                   "options": ["XS", "S", "M", "L", "XL", "0", "2", "4", "6", "8", "10", "12", "14"]},
    "swimsuit":  {"mode": "list", "label": "Sizes", "placeholder": "e.g. S, M, L or 4, 6, 8",
                   "options": ["XS", "S", "M", "L", "XL", "0", "2", "4", "6", "8", "10", "12", "14"]},
    "outerwear": {"mode": "list", "label": "Sizes", "placeholder": "e.g. S, M, L",
                   "options": ["XS", "S", "M", "L", "XL", "XXL", "3XL"]},
    "footwear":  {"mode": "list", "label": "Shoe size", "placeholder": "e.g. 8, 8.5, 9",
                   "options": ["5", "5.5", "6", "6.5", "7", "7.5", "8", "8.5", "9", "9.5", "10", "10.5", "11"]},
    "accessory": {"mode": "list", "label": "Size", "placeholder": "e.g. One size",
                   "options": ["One size", "OS"]},
}


def size_schema(category: str) -> dict:
    return SIZE_SCHEMAS.get(category or "", SIZE_SCHEMAS["top"])
COLOR_HEX = {
    "white": "#f2f2f2", "black": "#1a1a1a", "gray": "#8a8f98",  # 'grey' collapses to gray via COLOR_SYNONYMS
    "navy": "#1f2a44", "indigo": "#3b4a6b", "blue": "#3b5ba8", "light blue": "#9db8d9",
    "red": "#a33333", "green": "#2e4a3a",
    "beige": "#d9c9a3", "brown": "#6b4a2f", "tan": "#c8b98a", "pink": "#d9b3a0",
    "burgundy": "#6d2332", "purple": "#5b3a6d", "yellow": "#d9c04a", "orange": "#c96a2e",
    "teal": "#2c4f46", "cream": "#f2efe6", "khaki": "#c8b98a", "olive": "#6b7a3a",
}

# Map common spellings/close variants onto the canonical COLOR_HEX keys so a
# color is only ever stored one way. Free-text variants ("navy blue", "olive
# green", "grey") are the #1 cause of mismatching — the recommender and try-on
# compare these exact strings, so "navy blue" ≠ "navy" silently.
COLOR_SYNONYMS = {
    "grey": "gray", "charcoal": "gray", "charcoal gray": "gray", "charcoal grey": "gray",
    "silver": "gray", "slate": "gray", "heather gray": "gray", "heather grey": "gray",
    "light gray": "gray", "light grey": "gray", "dark gray": "gray", "dark grey": "gray",
    "navy blue": "navy", "dark navy": "navy", "midnight": "navy", "midnight blue": "navy",
    "olive green": "olive", "army green": "olive", "sage": "olive",
    "forest green": "green", "emerald": "green", "dark green": "green", "lime": "green",
    "royal blue": "blue", "dark blue": "blue", "steel blue": "blue", "cobalt": "blue",
    "sky blue": "light blue", "light blue": "light blue", "baby blue": "light blue",
    "ice blue": "light blue", "powder blue": "light blue", "pastel blue": "light blue",
    "denim blue": "indigo", "dark denim": "indigo", "indigo": "indigo", "denim": "indigo",
    "crimson": "red", "scarlet": "red", "dark red": "burgundy", "maroon": "burgundy",
    "wine": "burgundy", "bordeaux": "burgundy", "magenta": "pink", "hot pink": "pink",
    "rose": "pink", "fuchsia": "pink", "blush": "pink", "salmon": "pink",
    "lavender": "purple", "violet": "purple", "plum": "purple", "lilac": "purple",
    "mustard": "yellow", "gold": "yellow", "golden": "yellow", "sunflower": "yellow",
    "rust": "orange", "coral": "orange", "peach": "orange", "apricot": "orange",
    "turquoise": "teal", "aqua": "teal", "mint": "teal", "seafoam": "teal",
    "camel": "tan", "ivory": "cream", "off-white": "cream", "ecru": "cream",
    "khaki": "khaki", "tan": "tan", "olive": "olive",
}


def normalize_color(name: str) -> str:
    """Map a free-text color to the canonical palette ('' for blank). Compound
    'a&b' patterns collapse to the dominant color so they can't mismatch."""
    n = (name or "").strip().lower()
    if not n:
        return ""
    if n in COLOR_HEX:
        return n
    if n in COLOR_SYNONYMS:
        return COLOR_SYNONYMS[n]
    for sep in ("&", "+", " and ", " / "):
        parts = [p.strip() for p in n.split(sep) if p.strip()]
        if len(parts) >= 2 and parts[0] in COLOR_HEX:
            return parts[0]
    return n


# ---- machine-driven color (photo pixels) -----------------------------------
# The palette splits blues into navy / indigo / blue / light blue etc., but the
# AI tag-reader often returns just a coarse "blue" (denim has no color tag), and
# COLOR_SYNONYMS used to flatten every shade onto "blue". These helpers re-derive
# the specific shade from the garment PHOTO's pixels so a light-wash jean and a
# dark-wash jean don't both land on "blue". Pixels may only REFINE a coarse color
# within the same family (COLOR_REFINE_FAMILIES) — they never override a specific
# tag color ("navy" tag stays navy even if the photo reads brighter).

# blue-family shade boundaries (dominant-hue value → shade). Tuned against the
# real wardrobe photos: light-wash jeans ~v0.42–0.50, dark denim ~v0.31–0.35,
# tag-navy garments ~v0.26–0.35, bright/royal blue ~v0.68+.
_BLUE_SHADES = (
    (0.66, "blue"),        # bright / royal
    (0.40, "light blue"),  # pale sky / light-wash denim
    (0.27, "indigo"),      # dark-wash denim
    (0.0, "navy"),         # very dark blue
)

# coarse canonical color → the specific shades pixels are allowed to refine it to
COLOR_REFINE_FAMILIES = {
    "blue": {"light blue", "blue", "indigo", "navy"},
    "green": {"olive", "green", "teal"},
    "red": {"red", "burgundy"},
    "brown": {"brown", "tan", "beige", "khaki", "orange"},
    "purple": {"purple", "pink", "burgundy"},
    "yellow": {"yellow", "olive", "cream"},
}


def _hsv(r: int, g: int, b: int) -> tuple[float, float, float]:
    r, g, b = r / 255.0, g / 255.0, b / 255.0
    mx, mn = max(r, g, b), min(r, g, b)
    d = mx - mn
    v = mx
    s = 0.0 if mx == 0 else d / mx
    if d == 0:
        h = 0.0
    elif mx == r:
        h = 60 * (((g - b) / d) % 6)
    elif mx == g:
        h = 60 * (((b - r) / d) + 2)
    else:
        h = 60 * (((r - g) / d) + 4)
    return h % 360, s, v


def _shade_from_hsv(h: float, s: float, v: float) -> str:
    """Map a dominant (hue, sat, value) to the closest canonical palette key."""
    h = h % 360
    if 200 <= h < 260:  # blues — the fine split that matters most
        if v > 0.78 and s < 0.55:
            return "light blue"  # pale / sky
        for th, shade in _BLUE_SHADES:
            if v >= th:
                return shade
        return "navy"
    if 160 <= h < 200:
        return "teal"
    if 80 <= h < 160:
        return "olive" if s < 0.35 or v < 0.45 else "green"
    if 62 <= h < 80:
        return "olive" if v < 0.55 or s < 0.35 else "yellow"
    if 40 <= h < 62:
        return "olive" if v < 0.55 or s < 0.4 else "yellow"
    if 15 <= h < 40:
        return "orange" if v > 0.62 else "brown"
    if 290 <= h < 345:
        return "pink" if v > 0.5 else "burgundy"
    if 260 <= h < 290:
        return "purple"
    return "red" if v > 0.5 else "burgundy"  # h < 15 or h >= 345


def detect_color(data: bytes, crop_frac: float = 0.6) -> str:
    """Machine-driven canonical color from the garment PHOTO's pixels.

    Uses the same CENTER crop as the phash / near-dup gate. Achromatic garments
    resolve to white/gray/black by overall lightness; otherwise the dominant hue
    of the colorful pixels picks a specific palette shade. '' if unreadable."""
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
        w, h = img.size
        cw, ch = max(1, int(w * crop_frac)), max(1, int(h * crop_frac))
        left, top = (w - cw) // 2, (h - ch) // 2
        img = img.crop((left, top, left + cw, top + ch)).resize((48, 48))
        hsvs = [_hsv(*px) for px in img.getdata()]
        colorful = [(h, s, v) for h, s, v in hsvs if s >= 0.15 and 0.25 <= v <= 0.95]
        if len(colorful) < max(2, int(len(hsvs) * 0.12)):
            med = statistics.median(v for _, _, v in hsvs)
            if med > 0.85:
                return "white"
            if med < 0.30:
                return "black"
            return "gray"
        cos = sum(math.cos(math.radians(h)) for h, _, _ in colorful)
        sin = sum(math.sin(math.radians(h)) for h, _, _ in colorful)
        dom = (math.degrees(math.atan2(sin, cos)) + 360) % 360
        med_s = statistics.median(s for _, s, _ in colorful)
        med_v = statistics.median(v for _, _, v in colorful)
        return _shade_from_hsv(dom, med_s, med_v)
    except Exception:  # noqa: BLE001 — unreadable → no color
        return ""


def refine_color(coarse: str, data: bytes) -> str:
    """Refine a coarse canonical color using the photo's pixels, within the same
    family only. 'blue' → 'light blue' / 'indigo' / 'navy' when the pixels say so;
    a specific tag color ('navy', 'black', 'white', …) is never overridden."""
    base = normalize_color(coarse)
    if not base or base in ("black", "white"):
        return base
    family = COLOR_REFINE_FAMILIES.get(base)
    if not family:
        return base
    px = detect_color(data)
    return px if px in family else base


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


def normalize_orientation(data: bytes, rotate: int = 0) -> tuple[bytes, str]:
    """Right-side-up + portrait (vertical) for consistent display.

    Hard rules (deterministic, no exceptions):
      1. apply EXIF orientation (fixes sideways / upside-down phone photos)
      2. `rotate` ∈ {0, 90, 180, 270} from the tag reader — 180 flips an
         upside-down garment, 90/270 correct a sideways one.
      3. When NO rotation was chosen by the tag reader, never output a
         landscape (horizontal) image — a wide frame is rotated back to
         portrait. A 90/270 chosen by the tag reader is trusted even if it
         makes the frame horizontal (the garment is upright, which is what
         matters).

    Returns (normalized bytes, new ext) or (data, '') when nothing needed
    changing / the image can't be decoded (caller keeps it)."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:  # noqa: BLE001 — unreadable → leave as-is
        return data, ""
    changed = False
    try:
        if int(img.getexif().get(0x0112, 1)) != 1:
            img = ImageOps.exif_transpose(img)
            changed = True
    except Exception:  # noqa: BLE001 — no/odd EXIF → treat as upright
        pass
    if rotate == 180:
        img = img.transpose(Image.Transpose.ROTATE_180)
        changed = True
    elif rotate == 90:
        img = img.transpose(Image.Transpose.ROTATE_90)
        changed = True
    elif rotate == 270:
        img = img.transpose(Image.Transpose.ROTATE_270)
        changed = True
    if rotate == 0 and img.width > img.height:
        # heuristic fallback only when the tag reader found no rotation: never
        # leave a horizontal frame (phone shot sideways with no tag signal).
        img = img.transpose(Image.Transpose.ROTATE_90)
        changed = True
    if not changed:
        return data, ""
    buf = io.BytesIO()
    if img.mode in ("RGBA", "LA", "P"):
        img.convert("RGBA").save(buf, "PNG")
        return buf.getvalue(), "png"
    img.convert("RGB").save(buf, "JPEG", quality=88)
    return buf.getvalue(), "jpg"


def save_garment_image(user_id: int, garment_id: int, data: bytes, ext: str,
                       ai_orient: bool = False, rotate: int | None = None) -> Path:
    """Store a garment photo. Orientation is normalized on EVERY save (no
    exceptions): EXIF righting + portrait (unless the tag reader picked 90/270,
    which is trusted even if it makes the frame horizontal). When `ai_orient`
    is set (file uploads), the tag reader picks the rotation (0/90/180/270); an
    explicit `rotate` overrides that."""
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
    if rotate is None:
        rotate = aifill.ai_orientation(data) if ai_orient else 0
    data, norm_ext = normalize_orientation(data, rotate=rotate)
    if norm_ext:
        ext = norm_ext
    path = d / f"{garment_id}.{ext}"
    path.write_bytes(data)
    # record image + perceptual hash + color fingerprint for near-dup detection
    wardrobe.update(user_id, garment_id, image_path=path.name,
                    phash=phash.image_phash(data), color_sig=phash.image_color_class(data))
    return path


def _canonical_color(color_tags: str) -> str:
    """First canonical color tag, normalized ('navy,dark' -> 'navy'). Empty
    string when no canonical color is set (caller falls back to the photo-based
    coarse color class)."""
    if not color_tags:
        return ""
    return normalize_color(color_tags.split(",")[0].strip())


def near_duplicates(
    user_id: int,
    phash_hex: str,
    color_sig: str = "",
    exclude_id: int | None = None,
    threshold: int = phash.SIMILAR_THRESHOLD,
    category: str | None = None,
    color_tags: str = "",
) -> list[dict]:
    """Existing garments for this user that are likely the same (or nearly the
    same) item as one with `phash_hex`. Sorted by closest first.
    Empty list = no near-duplicate.

    Three gates (all must pass) — EXCEPT a near-identical picture (Hamming
    distance <= SAME_IMAGE_DISTANCE) which bypasses the category/color gates,
    since it is the same photo regardless of how it was tagged:
    - same `category` (a top vs. a pair of pants shot the same way isn't a dup)
    - color: when BOTH garments carry a canonical color tag, those must match
      (so a red one-piece is never "similar to" a pink polka-dot one even when
      their photo-based coarse color class collides); otherwise fall back to
      matching the coarse photo `color_sig`
    - center-crop dHash Hamming distance <= threshold
    """
    if not phash_hex:
        return []
    my_color = _canonical_color(color_tags)
    out: list[dict] = []
    for g in wardrobe.all(user_id):
        if not g.phash or g.id == exclude_id:
            continue
        d = phash.hamming(phash_hex, g.phash)
        if d > threshold:
            continue
        same_image = d <= phash.SAME_IMAGE_DISTANCE
        # a near-identical picture is the same item no matter how it was tagged
        # (same plaid shirt imported once as 'top' and once as 'outerwear')
        if not same_image and category is not None and g.category != category:
            continue
        g_color = _canonical_color(g.color_tags)
        if not same_image:
            if my_color and g_color:
                # both canonical → must be the same color (red != pink)
                if my_color != g_color:
                    continue
            elif not color_sig or not g.color_sig or g.color_sig != color_sig:
                continue
        out.append({"id": g.id, "name": g.name, "distance": d})
    out.sort(key=lambda x: x["distance"])
    return out


def nearest_dup(
    user_id: int, phash_hex: str, color_sig: str = "", exclude_id: int | None = None,
    threshold: int = phash.SIMILAR_THRESHOLD, category: str | None = None,
    color_tags: str = "",
) -> dict | None:
    """Closest match only (for the wardrobe grid's "similar to X" note)."""
    dups = near_duplicates(user_id, phash_hex, color_sig=color_sig,
                           exclude_id=exclude_id, threshold=threshold,
                           category=category, color_tags=color_tags)
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
    # images live under the OWNER's wardrobe dir — shared garments must resolve
    # to g.user_id (== user_id for the owner, owner for a family viewer)
    d["has_image"] = garment_image_path(g.user_id, g.id) is not None
    # nearest existing garment this one is a near-duplicate of (for "similar to
    # X" notes) — scoped to the owner's collection, same category + dominant color
    nd = (nearest_dup(g.user_id, g.phash, g.color_sig, exclude_id=g.id,
                      category=g.category, color_tags=g.color_tags)
          if g.phash else None)
    d["near_dup_of"] = nd
    return d
