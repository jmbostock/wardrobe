"""Image-link helpers: parse a store product page (HTML) and find the main
product image URL. HTTP fetching lives in main.py; these are pure functions so
they're easy to unit-test. BeautifulSoup is an optional dep (lazy import) —
extraction degrades gracefully if it's absent.
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# max images returned to the UI picker (product galleries can be long)
MAX_PRODUCT_IMAGES = 16

# common color words (longer phrases first so "navy blue" wins over "navy")
COLOR_WORDS = [
    "navy blue", "olive green", "charcoal gray", "charcoal grey", "navy", "black",
    "white", "blue", "gray", "grey", "red", "green", "beige", "brown", "tan",
    "pink", "burgundy", "purple", "yellow", "orange", "teal", "cream", "khaki",
    "olive", "charcoal", "heather", "maroon", "coral", "lilac", "mustard", "rust",
    "slate", "indigo", "camel", "plaid", "striped", "floral", "denim", "washed",
]

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "top": ["shirt", "tee", "top", "sweater", "hoodie", "turtleneck", "blouse",
            "polo", "crewneck", "henley", "tank", "cardigan", "sweatshirt"],
    "bottom": ["pant", "jean", "trouser", "short", "chino", "legging", "skirt", "jogger"],
    "dress": ["dress", "jumpsuit", "romper", "gown"],
    "outerwear": ["jacket", "coat", "puffer", "parka", "vest", "blazer", "windbreaker", "shell"],
    "footwear": ["shoe", "sneaker", "boot", "loafer", "heel", "sandal", "slip-on", "mule"],
    "accessory": ["hat", "beanie", "scarf", "belt", "sock", "glove", "bag", "handbag", "sunglasses"],
}




def is_image_bytes(data: bytes) -> bool:
    """True if the bytes look like a real image (PNG/JPEG/GIF/WebP)."""
    return bool(
        data[:8] == b"\x89PNG\r\n\x1a\n"
        or data[:3] == b"\xff\xd8\xff"
        or data[:6] in (b"GIF87a", b"GIF89a")
        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
    )


def detect_ext(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return "png"


def _looks_like_logo(url: str) -> bool:
    u = url.lower()
    # tp=nav / ?TP=NAV are navigation/marketing model shots, not the product
    return any(k in u for k in (".svg", "logo", "favicon", "spacer", "pixel", "transparent", "tp=nav"))


# Target long edge for garment images. Store CDNs (Gap Inc et al.) serve a tiny
# thumbnail when the URL has width/wid/w=N (e.g. ?width=92) — rewriting it up
# gives CatVTON real fabric detail instead of a mushy low-res blob.
TARGET_IMAGE_WIDTH = 1024

# Pure tracking/fingerprint noise in image URLs — safe to drop.
JUNK_QUERY_PARAMS = {
    "ts", "t", "timestamp", "v", "ver", "itok", "rand", "nonce",
    "srsltid", "gclid", "fbclid", "mc_eid", "msclkid",
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
}
WIDTH_QUERY_PARAMS = {"width", "wid", "w"}


def clean_image_url(url: str) -> str:
    """Normalize a store image URL for fetching: drop tracking/junk query
    params and rewrite width/wid/w=N up to TARGET_IMAGE_WIDTH (so we don't save
    92px thumbnails). Everything else is left intact — some CDNs return 404 if
    you remove params they expect. Returns the URL unchanged if it can't be
    parsed or has no query string."""
    if not url or url.startswith("data:"):
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.query:
        return url
    kept: list[tuple[str, str]] = []
    for k, v in parse_qsl(parts.query, keep_blank_values=True):
        kl = k.lower()
        if kl in JUNK_QUERY_PARAMS:
            continue
        if kl in WIDTH_QUERY_PARAMS:
            try:
                n = int(float(v))
            except ValueError:
                n = 0
            if n and n < TARGET_IMAGE_WIDTH:
                v = str(TARGET_IMAGE_WIDTH)
        kept.append((k, v))
    q = urlencode(kept) if kept else ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path, q, parts.fragment))


def jsonld_image_urls(node: Any) -> list[str]:
    """Collect image URLs from JSON-LD. Only accepts images attached to
    product/image-ish nodes (or typeless wrappers), so org/logo images that
    live elsewhere in the page don't leak in."""
    out: list[str] = []
    if isinstance(node, dict):
        t = str(node.get("@type") or "").lower()
        img = node.get("image")
        if img:
            urls: list[str] = []
            if isinstance(img, str):
                urls = [img]
            elif isinstance(img, list):
                urls = [x for x in img if isinstance(x, str)]
            elif isinstance(img, dict) and isinstance(img.get("url"), str):
                urls = [img["url"]]
            if urls and ("product" in t or "imageobject" in t or not t):
                out.extend(urls)
        for v in node.values():
            out.extend(jsonld_image_urls(v))
    elif isinstance(node, list):
        for v in node:
            out.extend(jsonld_image_urls(v))
    return out


def extract_image_url_from_html(html: str) -> str | None:
    """Best-effort: pull the main product image URL out of a store page's HTML.

    Retailers often set og:image to a *logo*, so we use it only as a last
    resort. Priority: JSON-LD product image → itemprop="image" → product
    gallery <img> (alt "Image number N showing, …") → largest product-ish
    <img> → og:image/twitter:image.
    Returns a raw URL (may be protocol-relative or relative to the page).
    """
    try:
        from bs4 import BeautifulSoup  # optional dep; graceful if absent
    except Exception:  # noqa: BLE001
        return None
    soup = BeautifulSoup(html, "html.parser")

    # 1) JSON-LD product image
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except Exception:  # noqa: BLE001
            continue
        for u in jsonld_image_urls(data):
            if u and not _looks_like_logo(u):
                return u

    # 2) itemprop="image"
    for img in soup.find_all("img", attrs={"itemprop": "image"}):
        src = img.get("src") or img.get("data-src")
        if src and not _looks_like_logo(src):
            return src

    # 3) product-gallery <img> (alt like "Image number 1 showing, ...")
    for img in soup.find_all("img"):
        alt = (img.get("alt") or "").lower()
        if re.search(r"image\s+number\s+\d+", alt):
            src = img.get("src") or img.get("data-src") or img.get("data-original")
            if src and not src.startswith("data:") and not _looks_like_logo(src):
                return src

    # 4) largest product-ish <img>
    candidates: list[tuple[int, str]] = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-original")
        if not src or src.startswith("data:") or _looks_like_logo(src):
            continue
        try:
            w = int(img.get("width") or img.get("data-width") or 0)
        except (TypeError, ValueError):
            w = 0
        hay = " ".join(str(x) for x in [img.get("class"), img.get("id"), src, img.get("alt")]).lower()
        score = w
        if any(k in hay for k in ("product", "pdp", "hero", "main", "zoom", "gallery", "model")):
            score += 500
        candidates.append((score, src))
    if candidates:
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    # 5) Open Graph / Twitter image (last resort — often a logo)
    for attrs in (
        {"property": "og:image"},
        {"name": "og:image"},
        {"property": "twitter:image"},
        {"name": "twitter:image"},
    ):
        m = soup.find("meta", attrs=attrs)
        if m and m.get("content"):
            return m["content"]
    return None


def _scan_jsonld(soup: Any) -> tuple[str, str, str, list[str]]:
    """Return (name, color, description, images) from JSON-LD product blocks."""
    name = color = desc = ""
    images: list[str] = []

    def walk(n: Any) -> None:
        nonlocal name, color, desc
        if isinstance(n, dict):
            t = str(n.get("@type") or "").lower()
            is_product = "product" in t or "offer" in t or "imageobject" in t or not t
            if is_product:
                if not name and isinstance(n.get("name"), str) and n["name"].strip():
                    name = n["name"].strip()
                if not desc and isinstance(n.get("description"), str) and n["description"].strip():
                    desc = n["description"].strip()
                if not color:
                    c = n.get("color")
                    if isinstance(c, str) and c.strip():
                        color = c.strip()
                    elif isinstance(c, list):
                        cs = [x for x in c if isinstance(x, str) and x.strip()]
                        if cs:
                            color = cs[0].strip()
                img = n.get("image")
                if isinstance(img, str):
                    images.append(img)
                elif isinstance(img, list):
                    images.extend(x for x in img if isinstance(x, str))
                elif isinstance(img, dict) and isinstance(img.get("url"), str):
                    images.append(img["url"])
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
        except Exception:  # noqa: BLE001
            continue
        walk(data)
    return name, color, desc, images


def _kw_re(word: str) -> re.Pattern:
    # match a keyword with an optional plural suffix (pant → pants, dress → dresses)
    return re.compile(r"\b" + re.escape(word) + r"(?:s|es)?\b")


def _guess_category(text: str) -> str | None:
    t = (text or "").lower()
    for cat, words in CATEGORY_KEYWORDS.items():
        for w in words:
            if _kw_re(w).search(t):
                return cat
    return None


def _scan_color(text: str) -> str | None:
    t = (text or "").lower()
    for c in COLOR_WORDS:
        if _kw_re(c).search(t):
            return c
    return None


def extract_product_page(html: str) -> dict:
    """Parse a product page → {name, description, color, category, images}.
    images are raw URLs (may be protocol-relative/relative); callers resolve
    them against the page URL. Deduped, logo-filtered, capped at 8."""
    out: dict = {"name": "", "description": "", "color": "", "category": None, "images": []}
    try:
        from bs4 import BeautifulSoup  # optional dep; graceful if absent
    except Exception:  # noqa: BLE001
        return out
    soup = BeautifulSoup(html, "html.parser")

    j_name, j_color, j_desc, j_images = _scan_jsonld(soup)

    # --- name ---
    name = j_name
    if not name:
        for attrs in (
            {"property": "og:title"}, {"name": "og:title"},
            {"property": "twitter:title"}, {"name": "twitter:title"},
        ):
            m = soup.find("meta", attrs=attrs)
            if m and m.get("content"):
                name = m["content"]
                break
    if not name:
        t = soup.find("title")
        if t and t.get_text(strip=True):
            name = t.get_text(strip=True)
    # strip trailing site names like " | Old Navy" / " - Old Navy"
    name = re.sub(r"\s+[-|]\s+[^|=-]{1,40}$", "", name).strip()
    out["name"] = name[:120]

    # --- description ---
    if j_desc:
        out["description"] = j_desc[:300]
    else:
        for attrs in ({"property": "og:description"}, {"name": "og:description"}, {"name": "description"}):
            m = soup.find("meta", attrs=attrs)
            if m and m.get("content"):
                out["description"] = m["content"].strip()[:300]
                break

    # --- color ---
    color = j_color
    if not color:
        hay = " ".join([out["name"], out["description"]])
        c = _scan_color(hay)
        if c:
            color = c
    out["color"] = color[:40]

    # --- category ---
    hay = " ".join([out["name"], out["description"]])
    out["category"] = _guess_category(hay)

    # --- images (JSON-LD → itemprop → gallery alt pattern → product-ish → og) ---
    seen: list[str] = []

    def add(u: str | None) -> None:
        if not u or u.startswith("data:") or _looks_like_logo(u) or u in seen:
            return
        seen.append(u)

    for u in j_images:
        add(u)
    for img in soup.find_all("img", attrs={"itemprop": "image"}):
        add(img.get("src") or img.get("data-src"))
    for img in soup.find_all("img"):
        if re.search(r"image\s+number\s+\d+", (img.get("alt") or "").lower()):
            add(img.get("src") or img.get("data-src") or img.get("data-original"))
    candidates: list[tuple[int, str]] = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-original")
        if not src or src.startswith("data:") or _looks_like_logo(src):
            continue
        try:
            w = int(img.get("width") or img.get("data-width") or 0)
        except (TypeError, ValueError):
            w = 0
        hay = " ".join(str(x) for x in [img.get("class"), img.get("id"), src, img.get("alt")]).lower()
        score = w
        if any(k in hay for k in ("product", "pdp", "hero", "main", "zoom", "gallery", "model", "outfit", "flat", "lifestyle")):
            score += 500
        # content CDN images (e.g. content.gapinc.com/d/...) are flat-lay / outfit
        # shots — include them, they're often the best try-on reference.
        if "/d/" in src or "?width=" in src:
            score += 300
        candidates.append((score, src))
    candidates.sort(key=lambda x: x[0], reverse=True)
    for _, src in candidates:
        add(src)
    for attrs in (
        {"property": "og:image"}, {"name": "og:image"},
        {"property": "twitter:image"}, {"name": "twitter:image"},
    ):
        m = soup.find("meta", attrs=attrs)
        if m:
            add(m.get("content"))

    out["images"] = seen[:MAX_PRODUCT_IMAGES]
    return out
