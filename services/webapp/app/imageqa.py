"""Image quality assessment for try-on inputs (person photo / garment image).

Pure-PIL heuristics (no ML): resolution, aspect ratio (full-body proxy),
brightness, sharpness, and a skin-tone check to flag model-wearing garment
shots vs flat-lays. Each check deducts from a 0-100 score and returns a
human-readable issue + tip. Thresholds are tuned conservatively.
"""
from __future__ import annotations

import io
from typing import Any

from PIL import Image, ImageFilter, ImageOps, ImageStat

PERSON_MIN_PX = 400       # smaller → "low resolution"
PERSON_IDEAL_PX = 768
DARK = 42
BRIGHT = 218
BLUR_BAD = 120            # edge variance below this = blurry
BLUR_SOFT = 350
SKIN_FRACTION_MODEL = 0.08


def _load(data: bytes) -> Image.Image | None:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        # Respect EXIF orientation: phones store portrait shots rotated (e.g.
        # 5312×2988 landscape + orientation=6). Without this, a portrait full-body
        # photo is scored as "nearly square/landscape" and unfairly penalized.
        return ImageOps.exif_transpose(img)
    except Exception:  # noqa: BLE001
        return None


def _analyze(img: Image.Image) -> dict[str, float]:
    small = img.convert("RGB")
    small.thumbnail((256, 256))
    g = small.convert("L")
    mean = ImageStat.Stat(g).mean[0]
    edge_var = ImageStat.Stat(g.filter(ImageFilter.FIND_EDGES)).var[0]
    return {"mean": float(mean), "edge_var": float(edge_var)}


def _skin_fraction(img: Image.Image) -> float:
    small = img.convert("RGB")
    small.thumbnail((160, 160))
    hsv = small.convert("HSV")
    n = total = 0
    for h, s, v in hsv.getdata():
        total += 1
        if (h <= 25 or h >= 335) and 40 <= s <= 175 and v >= 90:
            n += 1
    return n / total if total else 0


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


def _finish(score: int, issues: list[str], tips: list[str]) -> dict[str, Any]:
    score = max(0, min(100, score))
    return {
        "score": score,
        "grade": _grade(score),
        "issues": issues,
        "tips": tips or ["Looks good."],
    }


def assess_person(data: bytes) -> dict[str, Any]:
    img = _load(data)
    if img is None:
        return _finish(0, ["Couldn't read the image file"], ["Upload a valid JPG/PNG photo."])
    w, h = img.size
    ratio = w / h
    a = _analyze(img)
    score = 100
    issues: list[str] = []
    tips: list[str] = []

    if min(w, h) < PERSON_MIN_PX:
        score -= 25
        issues.append(f"Low resolution ({w}×{h}) — the result will look soft.")
        tips.append("Use a photo at least 768px tall (a phone photo is ideal).")
    elif min(w, h) < PERSON_IDEAL_PX:
        score -= 8
        issues.append(f"Resolution is a bit low ({w}×{h}).")
        tips.append("A taller / higher-res photo gives a sharper result.")

    if ratio > 0.95:
        score -= 25
        issues.append("Nearly square or landscape — probably not a full-body shot.")
        tips.append("Use a full-body photo: person head-to-toe, photo taller than wide.")
    elif ratio > 0.8:
        score -= 10
        issues.append("Fairly wide crop — may cut off the head or feet.")
        tips.append("Frame the whole body, head to feet.")

    if a["mean"] < DARK:
        score -= 20
        issues.append("Too dark — the body mask will be unreliable.")
        tips.append("Use a well-lit photo (avoid heavy backlighting).")
    elif a["mean"] > BRIGHT:
        score -= 15
        issues.append("Overexposed / washed out.")
        tips.append("Use a photo with normal exposure.")

    if a["edge_var"] < BLUR_BAD:
        score -= 25
        issues.append("Blurry / out of focus.")
        tips.append("Use a sharp, in-focus photo.")
    elif a["edge_var"] < BLUR_SOFT:
        score -= 8
        issues.append("A little soft / slightly blurry.")
        tips.append("A crisper photo lets the body mask be more accurate.")

    if not issues:
        tips = ["Looks great — full body, well-lit, sharp. Good to try on."]
    return _finish(score, issues, tips)


def assess_garment(data: bytes) -> dict[str, Any]:
    img = _load(data)
    if img is None:
        return _finish(0, ["Couldn't read the image file"], ["Use a valid garment photo (JPG/PNG)."])
    w, h = img.size
    a = _analyze(img)
    score = 100
    issues: list[str] = []
    tips: list[str] = []

    if min(w, h) < PERSON_MIN_PX:
        score -= 20
        issues.append(f"Low resolution ({w}×{h}) — fabric detail will be lost.")
        tips.append("Use a high-res garment photo (ideally 512px+).")
    if a["mean"] < DARK:
        score -= 15
        issues.append("Too dark.")
        tips.append("A well-lit garment photo masks better.")
    if a["edge_var"] < BLUR_BAD:
        score -= 20
        issues.append("Blurry garment image.")
        tips.append("Use a sharp photo of the garment.")
    if _skin_fraction(img) > SKIN_FRACTION_MODEL:
        score -= 20
        issues.append("Looks like it includes a person/model — a flat-lay (garment alone) works much better for try-on.")
        tips.append("Pick a flat-lay / plain product image rather than a model-wearing shot.")

    if not issues:
        tips = ["Looks good — a clean, flat garment image try-ons best."]
    return _finish(score, issues, tips)


def suitability(data: bytes, category: str | None = None) -> dict[str, Any]:
    """Try-on suitability score for a person photo (0-100) — used to auto-pick
    the best saved base photo for a garment swap.

    Reuses the person-QA heuristics (EXIF-aware aspect-ratio full-body proxy,
    resolution, brightness, sharpness) then nudges the score for the garment
    category being tried on:

      * top / outerwear — the face + upper body matter most, so wide crops that
        would cut the head are penalized harder
      * bottom / dress  — the whole body head-to-feet matters most, so wide
        crops that would cut the legs are penalized harder

    Returns {score, grade, reason, size:[w,h], ratio} (ratio < 1 → portrait)."""
    img = _load(data)
    if img is None:
        return {"score": 0, "grade": _grade(0),
                "reason": "Couldn't read the image file", "size": [0, 0], "ratio": 0.0}
    w, h = img.size
    ratio = w / h  # <1 → portrait (taller than wide)
    base = assess_person(data)
    score = int(base["score"])
    reason = base["issues"][0] if base["issues"] else "Good full-body photo"
    cat = (category or "").lower()
    if cat in ("top", "outerwear"):
        if ratio > 0.8:
            score -= 15
            reason = "Wide crop — a top try-on needs the face/upper body visible"
        elif 0.55 <= ratio <= 0.85:
            score += 5
            if score >= 70:
                reason = "Good framing for a top — face and upper body clear"
    elif cat in ("bottom", "dress"):
        if ratio > 0.8:
            score -= 15
            reason = "Wide crop — a bottom/dress try-on needs the legs/feet visible"
        elif ratio < 0.75:
            score += 5
            if score >= 70:
                reason = "Good full-body framing for a bottom/dress"
    score = max(0, min(100, score))
    return {"score": score, "grade": _grade(score), "reason": reason,
            "size": [w, h], "ratio": round(ratio, 3)}
