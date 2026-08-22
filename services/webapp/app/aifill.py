"""Optional AI metadata fill for garment uploads (file path).

When the user uploads a photo of a garment (e.g. a hanger shot with a visible
care/size tag), a small vision LLM via Ollama reads what it can — brand, color,
category, sizes — and returns it so the add-garment form can pre-fill the
fields. Pure heuristics (parse-link) already cover the URL path; this covers the
upload path with "AI fills what it can".

Graceful by design: if Ollama isn't running / no vision model is pulled, the
caller gets None and the UI simply leaves the fields manual. Nothing blocks on
this — never raise, always degrade.
"""
from __future__ import annotations

import base64
import collections
import json
import os
import re
from typing import Any

import httpx

from .config import settings

# qwen2.5vl:3b (~2GB) is the smallest Ollama vision model that reliably follows
# the labeled-line prompt AND reads tag text. moondream is ~0.3GB smaller but
# ignores multi-line instructions (hallucinates size lists), so it's not usable.
DEFAULT_VISION_MODEL = "qwen2.5vl:3b"
AI_FILL_TIMEOUT = 40  # seconds; tag reads should be fast, don't hang the form

# normalize model output to our fixed category set
CATEGORY_SYNONYMS: dict[str, str] = {
    "shirt": "top", "tee": "top", "t-shirt": "top", "tshirt": "top", "blouse": "top",
    "sweater": "top", "hoodie": "top", "cardigan": "top", "pullover": "top", "sweatshirt": "top",
    "pants": "bottom", "pant": "bottom", "jeans": "bottom", "trousers": "bottom",
    "shorts": "bottom", "skirt": "bottom", "leggings": "bottom",
    "dress": "dress", "gown": "dress", "jumpsuit": "dress",
    "jacket": "outerwear", "coat": "outerwear", "puffer": "outerwear", "parka": "outerwear",
    "vest": "outerwear", "blazer": "outerwear",
    "shoes": "footwear", "shoe": "footwear", "sneakers": "footwear", "boots": "footwear",
    "sandals": "footwear", "heels": "footwear",
    "hat": "accessory", "beanie": "accessory", "scarf": "accessory", "belt": "accessory",
    "bag": "accessory", "sunglasses": "accessory", "gloves": "accessory", "socks": "accessory",
}

# moondream (the default vision model) is tiny and does NOT reliably emit
# structured JSON — with `format:"json"` it invents its own keys. So we prompt
# for simple labeled lines (which it follows well) and parse those instead.
PROMPT = (
    "This is a photo of a clothing item, possibly on a hanger with a visible "
    "tag. Reply with EXACTLY five lines, one field per line, using these exact "
    "labels. Leave a value blank (nothing after the colon) when you cannot tell.\n"
    "NAME: <short garment name, e.g. Navy crewneck>\n"
    "BRAND: <brand name on a visible tag or label, else blank>\n"
    "COLOR: <main color word>\n"
    "CATEGORY: <top|bottom|dress|outerwear|footwear|accessory>\n"
    "SIZES: <sizes visible on a tag, comma separated like S,M,L or 8,10,12, "
    "else blank>"
)

_LINE_FIELDS = ["NAME", "BRAND", "COLOR", "CATEGORY", "SIZES"]

# vision models answer "I can't tell" in many ways — treat these as empty
_FILLER = {
    "blank", "none", "n/a", "na", "-", "--", "unknown", "unknown ",
    "not visible", "none visible", "not known", "not sure", "can't tell",
    "cannot tell", "cannot determine", "not applicable", "n/a ", "null",
}


def _clean(v: str) -> str:
    v = v.strip().strip('"')
    low = v.lower().strip(".")
    if (not low or low in _FILLER
            or low.startswith(("none", "not ", "no visible", "no brand", "no size"))):
        return ""
    return v


def _parse_labeled_lines(text: str) -> dict[str, str] | None:
    """Parse the NAME:/BRAND:/COLOR:/CATEGORY:/SIZES: line format moondream is
    asked to produce. Tolerates leading/following prose and extra whitespace."""
    out = {k: "" for k in _LINE_FIELDS}
    found = 0
    for line in text.splitlines():
        for f in _LINE_FIELDS:
            m = re.match(rf"^\s*{f}\s*:\s*(.*?)\s*$", line, re.I | re.S)
            if m:
                out[f] = m.group(1).strip()
                found += 1
                break
    if not found:
        return None
    return {k.lower(): v for k, v in out.items()}


def _normalize(out: dict[str, str]) -> dict[str, str]:
    res: dict[str, str] = {"name": "", "brand": "", "color": "", "category": "", "sizes": ""}
    for k in res:
        v = out.get(k)
        if isinstance(v, list):  # sizes sometimes come back as a list
            vals = [_clean(str(x)) for x in v if str(x).strip()]
            res[k] = ",".join(vals[:12])
        elif isinstance(v, (int, float)):
            res[k] = str(v)
        elif isinstance(v, str):
            res[k] = _clean(v)
    # normalize category to our fixed set
    cat = res["category"].strip().lower()
    if cat and cat not in CATEGORY_SYNONYMS.values():
        res["category"] = CATEGORY_SYNONYMS.get(cat, "")
    # normalize sizes: collapse whitespace, dedupe preserving order
    if res["sizes"]:
        parts = []
        for s in re.split(r"[,;]", res["sizes"]):
            s = re.sub(r"\s+", " ", s).strip()
            s = _clean(s)
            if s and s not in parts:
                parts.append(s)
        res["sizes"] = ",".join(parts[:12])
    return res


def parse_ai_fill(text: str) -> dict[str, str] | None:
    """Parse the model's raw reply into a clean {name, brand, color, category,
    sizes} dict. Tries JSON first (models that comply), then the labeled-line
    format moondream produces. Pure function, unit-testable."""
    if not text:
        return None
    t = text.strip()
    # --- try labeled lines first (moondream's format) ---
    lines = _parse_labeled_lines(t)
    if lines is not None:
        return _normalize(lines)
    # --- fall back to JSON (llava/qwen style models) ---
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    start = t.find("{")
    end = t.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(t[start : end + 1])
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    return _normalize(data)


_ORIENT_READ_PROMPT = (
    "Read the text on this garment's tag. If the text is NOT perfectly readable "
    "and right-side up, reply with exactly the single word 'none'. Otherwise "
    "reply with only the exact text you are sure of."
)


def _vlm_read(image_bytes: bytes, prompt: str) -> str:
    model = os.getenv("OLLAMA_VISION_MODEL", DEFAULT_VISION_MODEL).strip()
    url = f"{settings.ollama_url}/api/generate"
    payload = {
        "model": model, "prompt": prompt,
        "images": [base64.b64encode(image_bytes).decode("ascii")],
        "stream": False, "options": {"temperature": 0},
    }
    try:
        r = httpx.post(url, json=payload, timeout=AI_FILL_TIMEOUT)
        return (r.json() or {}).get("response", "") if r.status_code == 200 else ""
    except Exception:  # noqa: BLE001
        return ""


def ai_orientation(image_bytes: bytes) -> int:
    """'Rotate first, then read the text': try the 4 orientations and ask the
    vision model to read the tag text in each. If EXACTLY ONE orientation has
    readable text (the others say 'none'), return that rotation in degrees
    clockwise — that makes the garment upright. Returns 0 when ambiguous or no
    text is found, so the caller falls back to deterministic EXIF + portrait."""
    try:
        import io as _io
        from PIL import Image, ImageOps
        img = Image.open(_io.BytesIO(image_bytes)).convert("RGB")
        img.load()
        img = ImageOps.exif_transpose(img)
        img.thumbnail((1280, 1280))
    except Exception:  # noqa: BLE001 — unreadable → caller falls back
        return 0
    trans = {0: None, 90: Image.Transpose.ROTATE_270,
             180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_90}
    reads: dict[int, str] = {}
    for deg, t in trans.items():
        im = img.transpose(t) if t else img
        buf = _io.BytesIO()
        im.save(buf, "JPEG", quality=88)
        reads[deg] = _vlm_read(buf.getvalue(), _ORIENT_READ_PROMPT)
    good = []
    for deg, txt in reads.items():
        t = (txt or "").strip().strip(".")
        if t and t.lower() not in ("none", "no text", "n/a", "na"):
            good.append(deg)
    return good[0] if len(good) == 1 else 0


# ---------------------------------------------------------------------------
# "Look at it, then rotate" orientation (the reliable one).
# ---------------------------------------------------------------------------
# The tag-reader above is strong when a tag is readable, but useless on folded
# flat-lay photos with no readable tag (most of the wardrobe). Asking "is it
# upright?" also fails — a folded garment has no up/down so the model says YES
# at every rotation. The prompt below — "which edge is the garment's top on?" —
# proved stable (3/3 agreement) across every existing wardrobe photo, so we
# rotate to put that edge at the TOP. No readable tag needed.

_TOP_EDGE_PROMPT = (
    "This is a flat-lay photo of a clothing item on a surface. Look at the item "
    "as it would be worn upright. Which edge of the IMAGE is closest to the TOP "
    "of the garment (the neckline, waistband, or shoulder line)? Answer with "
    "exactly one word: top, bottom, left, right."
)
_TOP_EDGES = ("top", "bottom", "left", "right")
# clockwise degrees to bring a garment top from each edge to the TOP edge
_EDGE_TO_CW = {"top": 0, "bottom": 180, "left": 90, "right": 270}


def _top_edge(image_bytes: bytes) -> str:
    """Ask the vision model which image edge the garment's top is on. The model
    is small, so we vote up to 3 times (early-exit once 2 agree) and return the
    majority edge, or '' when it can't decide."""
    votes: list[str] = []
    for _ in range(3):
        txt = _vlm_read(image_bytes, _TOP_EDGE_PROMPT).strip().lower().strip(".")
        if txt in _TOP_EDGES:
            votes.append(txt)
        elif txt:
            first = txt.split()[0].rstrip(".")
            if first in _TOP_EDGES:
                votes.append(first)
        if len(votes) == 2 and votes[0] == votes[1]:
            break
    if not votes:
        return ""
    edge, n = collections.Counter(votes).most_common(1)[0]
    return edge if n >= 2 else ""


def ai_upright_rotation(image_bytes: bytes) -> int | None:
    """'Rotate it, then look': ask the model which edge the garment's top is on
    and return the clockwise rotation (0/90/180/270) that makes the garment
    upright (top at the top edge). Returns None when the model can't tell, so
    the caller falls back to deterministic EXIF + portrait."""
    try:
        import io as _io
        from PIL import Image, ImageOps
        img = Image.open(_io.BytesIO(image_bytes)).convert("RGB")
        img.load()
        img = ImageOps.exif_transpose(img)
        img.thumbnail((1280, 1280))
        buf = _io.BytesIO()
        img.save(buf, "JPEG", quality=88)
    except Exception:  # noqa: BLE001 — unreadable → caller falls back
        return None
    edge = _top_edge(buf.getvalue())
    if edge not in _EDGE_TO_CW:
        return None
    return _EDGE_TO_CW[edge]


def ai_fill_garment(image_bytes: bytes) -> dict[str, str] | None:
    """Read a garment photo with a vision LLM (Ollama) and return metadata.
    Returns None whenever the AI can't help (service down / no model / parse
    failure) so callers degrade gracefully."""
    model = os.getenv("OLLAMA_VISION_MODEL", DEFAULT_VISION_MODEL).strip()
    url = f"{settings.ollama_url}/api/generate"
    payload = {
        "model": model,
        "prompt": PROMPT,
        "images": [base64.b64encode(image_bytes).decode("ascii")],
        "stream": False,
        # NOTE: no `format:"json"` — moondream hallucinates its own schema there.
        "options": {"temperature": 0},
    }
    try:
        r = httpx.post(url, json=payload, timeout=AI_FILL_TIMEOUT)
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception:  # noqa: BLE001 — ollama down / timeout / unreachable
        return None
    text = (data or {}).get("response", "")
    parsed = parse_ai_fill(text)
    if parsed is None or not any(parsed.values()):
        return None
    return parsed
