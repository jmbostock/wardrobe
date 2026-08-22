"""Pick the best saved person photo as the base for trying on a specific garment.

The pick is driven by OUTFIT MATCH with the garment being tried on, not just
image geometry: a swimsuit garment should use a saved photo where the person is
already in a swimsuit / mostly-bare outfit, a full dress should use a dress
photo, and vice versa. We ask the Ollama vision LLM (same model as ai-fill) to
judge, in ONE multi-image call: the garment image plus every saved photo. When
the vision model is unavailable/unparseable we fall back to the pure-PIL
person-QA heuristic (imageqa.suitability), so the feature never blocks or breaks
(consistent with the app's graceful-degradation style).
"""
from __future__ import annotations

import base64
import io
import os
import re
from typing import Any

import httpx
from PIL import Image, ImageOps

from . import imageqa, photos
from .config import settings

# Same model family as ai-fill — small enough to run locally on the 5060 Ti but
# follows multi-line prompts and can describe/compare outfits from photos.
DEFAULT_VISION_MODEL = "qwen2.5vl:3b"
VISION_TIMEOUT = 60  # seconds; multi-image calls are slower than tag reads

# candidate photos are capped so the request stays small/fast
MAX_CANDIDATES = 10

PROMPT = (
    "You are choosing the best full-body photo of a person to use as the BASE "
    "image for a virtual try-on of a clothing item.\n\n"
    "The FIRST image is the garment to try on. The remaining images, in order, "
    "are candidate photos of the same person (candidate 1 is the first photo "
    "after the garment, candidate 2 the next, and so on).\n\n"
    "For EACH candidate reply with EXACTLY one line and nothing else, in this "
    "format:\n"
    "PHOTO <n>: <score 0-100> - <one short reason>\n\n"
    "Pick <score> mostly by how well the person's CURRENT outfit in that photo "
    "matches the garment's type and body coverage — e.g. a swimsuit or mostly-"
    "bare photo is a great base for a swimsuit, and a long dress photo is a bad "
    "base for a swimsuit (the reverse for a dress). Also reward: full body "
    "visible, upright, well-lit, sharp. Penalize: head or feet cropped, "
    "sideways, dark, blurry."
)

# "PHOTO 2: 88 - swimsuit base matches" (also tolerates a bare score, or a colon
# after the score instead of a dash, and extra prose lines before/after).
_PHOTO_LINE = re.compile(r"^\s*PHOTO\s+(\d+)\s*:\s*(\d{1,3})\b(.*)$", re.I)


def _thumb(data: bytes, max_px: int = 512) -> bytes:
    """EXIF-aware downscale to a small JPEG so the vision call stays fast."""
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    img.thumbnail((max_px, max_px), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=82)
    return buf.getvalue()


def _readable(data: bytes) -> bool:
    """True if the bytes decode to a non-empty image (corrupt files are skipped)."""
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
        return img.size[0] > 0 and img.size[1] > 0
    except Exception:  # noqa: BLE001
        return False


def _grade(score: int) -> str:
    if score >= 85:
        return "Great"
    if score >= 70:
        return "Good"
    if score >= 55:
        return "OK"
    if score >= 40:
        return "Not great"
    return "Poor"


def _parse_photo_lines(text: str) -> list[tuple[int, int, str]]:
    """Parse the model's 'PHOTO n: score - reason' lines into
    [(n, score, reason), ...] in output order."""
    out: list[tuple[int, int, str]] = []
    for m in _PHOTO_LINE.finditer(text or ""):
        n = int(m.group(1))
        score = int(m.group(2))
        reason = m.group(3).strip().lstrip("-: ").strip()
        out.append((n, score, reason))
    return out


def _vision_rank(
    garment_bytes: bytes, candidates: list[tuple[int, bytes]]
) -> dict[int, dict[str, Any]] | None:
    """One Ollama vision call (garment image + every saved photo) → per-photo
    {score, reason} keyed by photo id. Returns None when the model is down or
    the reply can't be parsed (caller falls back to the heuristic)."""
    if not candidates:
        return {}
    model = os.getenv("OLLAMA_VISION_MODEL", DEFAULT_VISION_MODEL).strip()
    try:
        images = [base64.b64encode(_thumb(garment_bytes)).decode("ascii")]
        for _pid, data in candidates[:MAX_CANDIDATES]:
            images.append(base64.b64encode(_thumb(data)).decode("ascii"))
        payload = {
            "model": model,
            "prompt": PROMPT,
            "images": images,
            "stream": False,
            "options": {"temperature": 0},
        }
        r = httpx.post(
            f"{settings.ollama_url}/api/generate", json=payload, timeout=VISION_TIMEOUT
        )
        if r.status_code != 200:
            return None
        text = (r.json() or {}).get("response", "")
    except Exception:  # noqa: BLE001 — ollama down / timeout / bad image bytes
        return None
    out: dict[int, dict[str, Any]] = {}
    for n, score, reason in _parse_photo_lines(text):
        idx = n - 1  # candidate numbering is 1-based; photos start at index 1
        if 0 <= idx < len(candidates):
            pid = candidates[idx][0]
            out[pid] = {
                "score": max(0, min(100, score)),
                "reason": (reason or "outfit matches this garment")[:160],
            }
    return out or None


def rank_photos_for_garment(
    user_id: int, garment_bytes: bytes, garment_category: str
) -> list[dict[str, Any]]:
    """Rank the user's saved person photos best-first as the try-on base for a
    specific garment. Primary signal = outfit match via vision LLM; falls back
    to the pure-PIL person-QA heuristic (category-nudged) when the model is
    unavailable. Corrupt/undecodable photos are skipped — never offered.

    Returns photo dicts (photos._row_to_dict fields) plus score/grade/reason/
    method ("ai" | "heuristic")."""
    candidates: list[tuple[dict[str, Any], bytes]] = []
    for p in photos.list(user_id):
        try:
            data = photos.photo_bytes(user_id, p["id"])
        except photos.PhotoError:
            continue
        if _readable(data):
            candidates.append((p, data))
    if not candidates:
        return []

    method = "ai"
    scores = _vision_rank(garment_bytes, [(p["id"], d) for p, d in candidates])
    if scores is None:
        method = "heuristic"
        scores = {}
        for p, d in candidates:
            s = imageqa.suitability(d, garment_category)
            scores[p["id"]] = {"score": s["score"], "reason": s["reason"]}

    ranked: list[dict[str, Any]] = []
    for p, _d in candidates:
        sc = scores.get(p["id"])
        if not sc:
            continue
        ranked.append(
            {**p, "score": sc["score"], "grade": _grade(sc["score"]),
             "reason": sc["reason"], "method": method}
        )
    ranked.sort(key=lambda r: r["score"], reverse=True)
    return ranked
