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

DEFAULT_VISION_MODEL = "moondream"  # tiny (~1.7GB), reads tags fine, CPU-friendly
AI_FILL_TIMEOUT = 25  # seconds; tag reads should be fast, don't hang the form

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

PROMPT = (
    "You are looking at a photo of a clothing item. Return JSON only, no prose.\n"
    '{"name": short garment name, "brand": brand name if visible on a tag or label '
    'else "", "color": main color word, "category": one of "top","bottom","dress",'
    '"outerwear","footwear","accessory", "sizes": comma-separated sizes visible on a '
    'tag (e.g. "S,M,L" or "8,10,12") else ""}.\n'
    "Only include values you can see or reasonably infer from the garment itself. "
    "Use empty strings for anything unknown. No extra text."
)


def parse_ai_fill(text: str) -> dict[str, str] | None:
    """Parse the model's raw reply into a clean {name, brand, color, category,
    sizes} dict. Tolerates code fences / leading prose. Pure function, unit-testable."""
    if not text:
        return None
    t = text.strip()
    # strip markdown code fences
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    # find the first balanced {...} object
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

    out: dict[str, str] = {"name": "", "brand": "", "color": "", "category": "", "sizes": ""}
    for k in out:
        v = data.get(k)
        if isinstance(v, str):
            out[k] = v.strip()
        elif isinstance(v, (int, float)):
            out[k] = str(v).strip()
        elif isinstance(v, list):  # sizes sometimes come back as a list
            out[k] = ", ".join(str(x).strip() for x in v if str(x).strip())

    # normalize category to our fixed set
    cat = out["category"].strip().lower()
    if cat and cat not in CATEGORY_SYNONYMS.values():
        out["category"] = CATEGORY_SYNONYMS.get(cat, "")
    # normalize sizes: collapse whitespace, dedupe preserving order
    if out["sizes"]:
        parts = []
        for s in re.split(r"[,;]", out["sizes"]):
            s = re.sub(r"\s+", " ", s).strip()
            if s and s not in parts:
                parts.append(s)
        out["sizes"] = ",".join(parts[:12])
    return out


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
        "format": "json",
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
