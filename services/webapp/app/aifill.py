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
