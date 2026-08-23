"""User style profile (optional "bio") + hidden derived profile.

The Account UI collects a handful of OPTIONAL fields ("bio") that give the
recommender a cold-start picture of the person. `derive_profile()` turns those
raw answers into a HIDDEN, computed profile (size buckets, formality zone,
guardrails, occasion weights, ...) that the recommendation engine and stylist
consume to build responses. It is intentionally NOT returned to the browser
(the user wants it tracked but not shown); `auth.public_user()` strips it.
Recomputed on every profile save (later also on feedback).

Everything here is optional; most is also learned from feedback over time — the
bio just accelerates cold start and encodes hard guardrails learning can't fix
(e.g., "it doesn't fit me", "no yellow").

Storage: two JSON columns on `users`:
  - `profile`         raw normalized bio answers (editable, returned to the UI)
  - `derived_profile` computed profile (server-side only — stylist/recommender)
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from . import db, media

# -- allowed bio keys: key -> value kind -------------------------------------
#   str  : trimmed string (bounded length)
#   csv  : "a, b, c" -> "a, b, c" (deduped, normalized spaces)
#   sex  : '' | 'm' | 'f'
#   warmth: '' | -1 (runs hot) | 0 (neutral) | +1 (runs cold)
#   formality: '' | casual | smart-casual | business | formal
BIO_FIELDS: dict[str, str] = {
    "bio": "str",            # free-text note
    "sex": "sex",            # '' | 'm' | 'f'
    "height": "str",         # free text e.g. "5'10\"" or "178cm"
    "top_size": "str",       # S/M/L/XL (or numeric)
    "bottom_size": "str",    # waist × length, e.g. "30W x 32L"
    "shoe_size": "str",      # e.g. "10.5"
    "warmth_bias": "warmth", # +1 runs cold, -1 runs hot, 0 neutral
    "formality_min": "formality",
    "formality_max": "formality",
    "never_wear": "csv",     # "shorts, patterns, yellow"
    "style_keywords": "csv", # "minimal, preppy, athleisure"
    "occasions": "str",      # free text: "office 5x/wk, gym 3x/wk"
    "fav_colors": "csv",
    "colors_avoid": "csv",
    "age_range": "str",      # '' | under20 | 20s | 30s | 40s | 50+
}

FORMALITY = ("", "casual", "smart-casual", "business", "formal")
FORMALITY_ORDER = ["casual", "smart-casual", "business", "formal"]

# recommended-to-fill fields for the cold-start "completeness" score
RECOMMENDED_FIELDS = (
    "sex", "height", "top_size", "bottom_size", "shoe_size",
    "warmth_bias", "formality_min", "formality_max", "never_wear",
)

# words that imply a PATTERN guardrail when they appear in never_wear
_PATTERN_WORDS = {"pattern", "patterns", "print", "prints", "graphic", "graphics",
                  "stripes", "striped", "plaid", "floral", "polka", "polka-dot",
                  "check", "checks", "gingham", "camouflage", "camouflage-print"}
# clothing-type words -> guardrail tag (recommender matches against name/category)
_TYPE_GUARDRAILS = {
    "shorts": "no_shorts", "skirts": "no_skirts", "skirt": "no_skirts",
    "dresses": "no_dresses", "dress": "no_dresses",
    "tank": "no_tank", "sleeveless": "no_tank", "tanks": "no_tank",
    "hoodies": "no_hoodies", "hoodie": "no_hoodies",
    "sandals": "no_sandals", "flip-flops": "no_sandals", "flip flops": "no_sandals",
}

_STR_MAX = 200
_CSV_MAX = 200


def _clean(v: Any, maxlen: int = _STR_MAX) -> str:
    return (str(v) if v is not None else "").strip()[:maxlen]


def _csv(v: Any, maxlen: int = _CSV_MAX) -> str:
    seen: list[str] = []
    for part in _clean(v).split(","):
        p = part.strip()
        if p and p not in seen:
            seen.append(p)
    return ", ".join(seen)[:maxlen]


def _normalize_value(key: str, kind: str, v: Any) -> str:
    if kind in ("str",):
        return _clean(v)
    if kind == "csv":
        return _csv(v)
    if kind == "sex":
        s = _clean(v).lower()
        return s if s in ("m", "f") else ""
    if kind == "warmth":
        s = _clean(v).lower()
        mapping = {"cold": "1", "runs cold": "1", "1": "1", "+1": "1",
                   "hot": "-1", "runs hot": "-1", "-1": "-1",
                   "neutral": "0", "0": "0", "": "0"}
        return mapping.get(s, "0")
    if kind == "formality":
        s = _clean(v).lower()
        return s if s in FORMALITY else ""
    return _clean(v)


def normalize_profile(raw: dict | None) -> dict:
    """Whitelist + normalize the submitted bio dict. Unknown keys dropped."""
    raw = raw or {}
    out: dict[str, str] = {}
    for key, kind in BIO_FIELDS.items():
        if key in raw:
            out[key] = _normalize_value(key, kind, raw.get(key))
    return out


# --------------------------------------------------------------------------- #
# derivation (the engine consumes this; also returned to the client)         #
# --------------------------------------------------------------------------- #

_HEIGHT_FT_IN_RE = re.compile(
    r"(\d+)\s*(?:ft|'|\u2032)\s*(?:(\d+)\s*(?:in|\"|''|\u2033))?"
)
_HEIGHT_CM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:cm|centimeters?|centimetres?)", re.I)
_HEIGHT_BARE_RE = re.compile(r"(\d+(?:\.\d+)?)")


def height_to_cm(h: str | None) -> float | None:
    """Parse a height string to cm. Accepts 5'10\", 178cm, 178, 5ft10in."""
    h = (h or "").strip()
    if not h:
        return None
    m = _HEIGHT_FT_IN_RE.match(h)
    if m:
        feet = int(m.group(1))
        inches = int(m.group(2) or 0)
        return round((feet * 12 + inches) * 2.54, 1)
    m = _HEIGHT_CM_RE.search(h)
    if m:
        return round(float(m.group(1)), 1)
    m = _HEIGHT_BARE_RE.search(h)
    if m:
        v = float(m.group(1))
        # bare number: >=120 assumed cm, otherwise inches
        return round(v if v >= 120 else v * 2.54, 1)
    return None


def _body_build(height_cm: float | None) -> str | None:
    if height_cm is None:
        return None
    if height_cm < 155:
        return "petite"
    if height_cm > 183:
        return "tall"
    return "average"


def _waist_in(bottom_size: str) -> int | None:
    m = re.search(r"(\d{2,3})\s*w(?:aist)?", bottom_size, re.I)
    if m:
        return int(m.group(1))
    m = re.match(r"(\d{2,3})", bottom_size.strip())
    return int(m.group(1)) if m else None


def _top_bucket(top_size: str) -> str | None:
    m = re.search(r"\b(x{0,2}s|x{0,2}l|m)\b", top_size, re.I)
    if not m:
        return None
    b = m.group(1).lower()
    order = ("xxs", "xs", "s", "m", "l", "xl", "xxl")
    return b if b in order else None


def _shoe_size(shoe_size: str) -> str | None:
    s = shoe_size.strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        return s
    return None


def _guardrails(never_wear: str) -> list[str]:
    """Turn the free-text never-wear list into machine guardrail tags.

    Examples:
      "shorts, patterns" -> ["no_shorts", "no_patterns"]
      "yellow"           -> ["avoid_color:yellow"]
      "floral, plaid"    -> ["no_patterns"]
    """
    guards: list[str] = []
    seen: set[str] = set()
    for token in (t.strip().lower() for t in never_wear.split(",") if t.strip()):
        if token in _PATTERN_WORDS:
            tag = "no_patterns"
        elif token in _TYPE_GUARDRAILS:
            tag = _TYPE_GUARDRAILS[token]
        else:
            col = media.normalize_color(token)
            tag = f"avoid_color:{col}" if col else f"never:{token}"
        if tag not in seen:
            seen.add(tag)
            guards.append(tag)
    return guards


def _style_tags(style_keywords: str) -> list[str]:
    return [t.strip().lower() for t in style_keywords.split(",") if t.strip()]


def _occasion_weights(occasions: str) -> dict[str, float]:
    out: dict[str, float] = {}
    if not occasions:
        return out
    for part in occasions.split(","):
        m = re.search(r"([a-zA-Z][a-zA-Z \-']*?)\s*(\d+(?:\.\d+)?)\s*x", part)
        if m:
            name = m.group(1).strip().lower()
            out[name] = float(m.group(2))
    return out


def _formality_zone(p: dict) -> dict:
    lo = p.get("formality_min") or "casual"
    hi = p.get("formality_max") or "formal"
    if lo not in FORMALITY_ORDER:
        lo = "casual"
    if hi not in FORMALITY_ORDER:
        hi = "formal"
    if FORMALITY_ORDER.index(lo) > FORMALITY_ORDER.index(hi):
        lo, hi = hi, lo
    return {"min": lo, "max": hi, "range": FORMALITY_ORDER.index(hi) - FORMALITY_ORDER.index(lo)}


def _completeness(p: dict) -> float:
    filled = sum(1 for f in RECOMMENDED_FIELDS if p.get(f))
    return round(filled / len(RECOMMENDED_FIELDS), 2)


def derive_profile(p: dict) -> dict:
    """Compute the derived profile from the raw bio answers."""
    height_cm = height_to_cm(p.get("height"))
    try:
        warmth = int(p.get("warmth_bias") or 0)
        warmth = max(-1, min(1, warmth))
    except (TypeError, ValueError):
        warmth = 0
    fav = [c.strip().lower() for c in (p.get("fav_colors") or "").split(",") if c.strip()]
    avoid = [c.strip().lower() for c in (p.get("colors_avoid") or "").split(",") if c.strip()]
    return {
        "version": 1,
        "computed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "completeness": _completeness(p),
        "sex": p.get("sex") or None,
        "height_cm": height_cm,
        "body_build": _body_build(height_cm),
        "size_buckets": {
            "top": _top_bucket(p.get("top_size") or ""),
            "waist_in": _waist_in(p.get("bottom_size") or ""),
            "shoe": _shoe_size(p.get("shoe_size") or ""),
        },
        "warmth_bias": warmth,
        "formality_zone": _formality_zone(p),
        "guardrails": _guardrails(p.get("never_wear") or ""),
        "style_tags": _style_tags(p.get("style_keywords") or ""),
        "occasion_weights": _occasion_weights(p.get("occasions") or ""),
        "palette": {"fav": fav, "avoid": avoid},
    }


# --------------------------------------------------------------------------- #
# persistence                                                                 #
# --------------------------------------------------------------------------- #

def save_profile(user_id: int, raw: dict | None) -> dict:
    """Normalize + persist bio, recompute + persist the derived profile."""
    conn = db.init()
    p = normalize_profile(raw)
    derived = derive_profile(p)
    with db.lock():
        conn.execute(
            "UPDATE users SET profile=?, derived_profile=? WHERE id=?",
            (json.dumps(p), json.dumps(derived), user_id),
        )
        conn.commit()
    return p


def load_profile(user_id: int) -> dict:
    conn = db.init()
    with db.lock():
        row = conn.execute("SELECT profile FROM users WHERE id=?", (user_id,)).fetchone()
    if not row or not row["profile"]:
        return {}
    try:
        return json.loads(row["profile"])
    except (json.JSONDecodeError, TypeError):
        return {}


def load_derived(user_id: int) -> dict:
    conn = db.init()
    with db.lock():
        row = conn.execute(
            "SELECT derived_profile FROM users WHERE id=?", (user_id,)
        ).fetchone()
    if not row or not row["derived_profile"]:
        return {}
    try:
        return json.loads(row["derived_profile"])
    except (json.JSONDecodeError, TypeError):
        return {}
