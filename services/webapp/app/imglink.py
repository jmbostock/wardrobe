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
    "navy blue", "olive green", "charcoal gray", "charcoal grey", "light blue",
    "navy", "indigo", "denim blue", "black",
    "white", "blue", "gray", "grey", "red", "green", "beige", "brown", "tan",
    "pink", "burgundy", "purple", "yellow", "orange", "teal", "cream", "khaki",
    "olive", "charcoal", "heather", "maroon", "coral", "lilac", "mustard", "rust",
    "slate", "camel", "plaid", "striped", "floral", "denim", "washed",
]

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "top": ["shirt", "tee", "top", "sweater", "hoodie", "turtleneck", "blouse",
            "polo", "crewneck", "henley", "tank", "cardigan", "sweatshirt"],
    "bottom": ["pant", "jean", "trouser", "short", "chino", "legging", "skirt", "jogger"],
    "dress": ["dress", "jumpsuit", "romper", "gown"],
    "swimsuit": ["swimsuit", "swimwear", "swim", "bikini", "tankini", "one-piece",
                  "one piece", "trunks", "boardshorts", "board shorts", "rash guard", "rashguard"],
    "bra": ["bra", "bralette", "bustier", "corset", "sports bra", "padded bra",
            "underwire", "triangle bra", "strapless bra"],
    "outerwear": ["jacket", "coat", "puffer", "parka", "vest", "blazer", "windbreaker", "shell"],
    "footwear": ["shoe", "sneaker", "boot", "loafer", "heel", "sandal", "slip-on", "mule"],
    "accessory": ["hat", "beanie", "scarf", "belt", "sock", "glove", "bag", "handbag", "sunglasses"],
}




def is_image_bytes(data: bytes) -> bool:
    """True if the bytes look like a real image (PNG/JPEG/GIF/WebP/HEIC)."""
    return bool(
        data[:8] == b"\x89PNG\r\n\x1a\n"
        or data[:3] == b"\xff\xd8\xff"
        or data[:6] in (b"GIF87a", b"GIF89a")
        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
        or _is_heic(data)
    )


# ISO-BMFF container brands for HEIC (iPhone) — and AVIF as a bonus.
_HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1", b"avif"}


def _is_heic(data: bytes) -> bool:
    return len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in _HEIC_BRANDS


def detect_ext(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if _is_heic(data):
        return "heic"
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


def _scan_jsonld(soup: Any) -> tuple[str, str, str, str, list[str], list[str]]:
    """Return (name, color, description, brand, sizes, images) from JSON-LD."""
    name = color = desc = brand = ""
    sizes: list[str] = []
    images: list[str] = []

    def push_size(v: str) -> None:
        v = v.strip(" :")
        if v and v.lower() not in (x.lower() for x in sizes) and _looks_like_size(v):
            sizes.append(v)

    def add_size(s: Any) -> None:
        if isinstance(s, str):
            raw = [s]
        elif isinstance(s, list):
            raw = [str(x) for x in s if x is not None]
        else:
            return
        for chunk in raw:
            chunk = chunk.strip()
            if not chunk:
                continue
            # whole-token matches first ("One Size", "28x32", "S")
            if _looks_like_size(chunk):
                push_size(chunk)
                continue
            # otherwise "29,30,31" / "Size: M" style → split and test each piece
            for v in re.split(r"[,;\s]+", chunk):
                push_size(v)

    def walk(n: Any) -> None:
        nonlocal name, color, desc, brand
        if isinstance(n, dict):
            t = str(n.get("@type") or "").lower()
            is_product = "product" in t or "offer" in t or "imageobject" in t or not t
            if is_product:
                if not name and isinstance(n.get("name"), str) and n["name"].strip():
                    name = n["name"].strip()
                if not desc and isinstance(n.get("description"), str) and n["description"].strip():
                    desc = n["description"].strip()
                if not brand:
                    b = n.get("brand")
                    if isinstance(b, str) and b.strip():
                        brand = b.strip()
                    elif isinstance(b, dict) and isinstance(b.get("name"), str) and b["name"].strip():
                        brand = b["name"].strip()
                if not color:
                    c = n.get("color")
                    if isinstance(c, str) and c.strip():
                        color = c.strip()
                    elif isinstance(c, list):
                        cs = [x for x in c if isinstance(x, str) and x.strip()]
                        if cs:
                            color = cs[0].strip()
                add_size(n.get("size"))
                # offers may carry the size per offer (name like "Size: M", sku, or size)
                offers = n.get("offers")
                offer_list = offers if isinstance(offers, list) else [offers] if isinstance(offers, dict) else []
                for o in offer_list:
                    if not isinstance(o, dict):
                        continue
                    add_size(o.get("size"))
                    for k in ("name", "sku"):
                        v = o.get(k)
                        if isinstance(v, str) and v.strip():
                            add_size(v)
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
    return name, color, desc, brand, sizes, images


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


def _looks_like_size(s: str) -> bool:
    """True if a token looks like a clothing size (S/M/L/XL, 0-30, 36-46 EU,
    28x32 pants, 30W x 32L, 34/32, shoe 8.5). Deliberately conservative so
    random description numbers don't leak in."""
    t = s.strip().upper()
    if not t or len(t) > 12:
        return False
    if re.fullmatch(r"(?:XS|S|M|L|XL|XXL|XXXL|2XL|3XL|4XL|5XL)", t):
        return True
    # single numeric apparel / EU / shoe size (allow one decimal for half sizes)
    if re.fullmatch(r"[0-9]{1,2}(?:\.5)?", t):
        n = float(t)
        return 0 <= n <= 60
    # pants: 28x32, 30/32, 28W x 32L, 30W / 32L, 30X32
    if re.fullmatch(r"[0-9]{1,2}\s*[X/W]\s*[0-9]{1,2}(?:\s*[WL])?", t):
        return True
    if re.fullmatch(r"[0-9]{1,2}\s*/\s*[0-9]{1,2}", t):
        return True
    # one-size
    if t in ("ONE SIZE", "ONESIZE", "OS", "OSFM"):
        return True
    return False


def _scan_html_sizes(soup: Any) -> list[str]:
    """Collect sizes from HTML size pickers (<select> or buttons whose
    id/class/aria-label mentions 'size'), plus elements tagged data-size."""
    out: list[str] = []

    def add(s: str | None) -> None:
        if not s:
            return
        s = s.strip()
        if s and s.lower() not in (x.lower() for x in out) and _looks_like_size(s):
            out.append(s)

    def hints(e: Any) -> str:
        return " ".join(str(x) for x in (
            e.get("id"), e.get("class"), e.get("aria-label"),
            e.get("data-testid"), e.parent.get("aria-label") if e.parent else None,
        )).lower()

    for sel in soup.find_all("select"):
        if "size" in hints(sel):
            for opt in sel.find_all("option"):
                add(opt.get("value") or opt.get_text(strip=True))
    for el in soup.find_all(["button", "a", "div", "span", "li"]):
        text = el.get_text(" ", strip=True)
        if not _looks_like_size(text) or len(text) > 12:
            continue
        ctx = hints(el) or (el.parent.get("class") if el.parent else None)
        hay = " ".join(str(x) for x in ([ctx] if not isinstance(ctx, str) else [ctx]))
        if "size" in hay or el.get("data-size"):
            add(text)
    # data-size / data-value on any element (swatches often drop the text)
    for el in soup.find_all(attrs={"data-size": True}):
        add(el.get("data-size"))
    return out[:20]


def _scan_brand(soup: Any) -> str:
    """Brand from meta tags (og:site_name / twitter:site) when JSON-LD lacks it."""
    for attrs in ({"property": "og:site_name"}, {"name": "og:site_name"},
                  {"name": "twitter:site"}, {"property": "twitter:site"}):
        m = soup.find("meta", attrs=attrs)
        if m and m.get("content"):
            b = m["content"].strip().lstrip("@")
            if b:
                return b[:120]
    return ""


def extract_product_page(html: str) -> dict:
    """Parse a product page → {name, description, color, category, brand,
    sizes, images}. images are raw URLs (may be protocol-relative/relative);
    callers resolve them against the page URL. Deduped, logo-filtered."""
    out: dict = {"name": "", "description": "", "color": "", "category": None,
                 "brand": "", "sizes": "", "images": []}
    try:
        from bs4 import BeautifulSoup  # optional dep; graceful if absent
    except Exception:  # noqa: BLE001
        return out
    soup = BeautifulSoup(html, "html.parser")

    j_name, j_color, j_desc, j_brand, j_sizes, j_images = _scan_jsonld(soup)

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

    # --- brand (JSON-LD brand → og:site_name) ---
    brand = j_brand
    if not brand:
        brand = _scan_brand(soup)
    out["brand"] = brand[:120]

    # --- sizes (JSON-LD size/offers → HTML size pickers) ---
    sizes = list(j_sizes)
    for s in _scan_html_sizes(soup):
        if s.lower() not in (x.lower() for x in sizes):
            sizes.append(s)
    out["sizes"] = ",".join(sizes[:12])

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
