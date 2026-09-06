"""Rule-based outfit recommender — the MVP core.

Deterministic, CPU-only, <10ms, fully explainable.
Scoring spec: docs/recommender.md

    score(g) =
        + 40 * warmth_match
        + 20 * formality_match
        + 10 * occasion_match
        + 10 * waterproof_bonus   (only when precipitating)
        - 15 * waterproof_penalty (outerwear/top not waterproof while precipitating)
        +  8 * harmony(top, bottom)
        +  6 * rotation_bonus
        +  5 * prompt_bonus
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import interactions, sharing
from .wardrobe import Garment, Wardrobe

FORMALITY_ORDER = ["casual", "smart-casual", "business", "formal"]

# activity -> (formality_level, occasion tags)
ACTIVITY_MAP = {
    "office": ("business", ["office"]),
    "work": ("business", ["office"]),
    "interview": ("business", ["office"]),
    "date": ("smart-casual", ["date", "event"]),
    "dinner": ("smart-casual", ["date", "event"]),
    "night": ("smart-casual", ["date", "event"]),
    "casual": ("casual", ["casual"]),
    "errands": ("casual", ["casual"]),
    "home": ("casual", ["casual"]),
    "hiking": ("casual", ["active"]),
    "gym": ("casual", ["active"]),
    "beach": ("casual", ["beach"]),  # own occasion so swimwear/beachwear beats generic active
    "wedding": ("formal", ["event"]),
    "gala": ("formal", ["event"]),
    "formal": ("formal", ["event"]),
}

RAIN_CONDITIONS = {"rain", "sleet", "snow", "thunderstorm", "snowy", "rainy"}


@dataclass
class Weather:
    temp_c: float
    feels_like_c: float | None = None
    condition: str = "clear"
    wind_kph: float = 0.0
    humidity: int = 50
    uv_index: float = 0.0

    @property
    def feels(self) -> float:
        return self.feels_like_c if self.feels_like_c is not None else self.temp_c

    @property
    def temp_f(self) -> float:
        return self.temp_c * 9 / 5 + 32

    @property
    def feels_like_f(self) -> float:
        return self.feels * 9 / 5 + 32

    @property
    def precipitating(self) -> bool:
        return self.condition.lower() in RAIN_CONDITIONS

    def to_dict(self) -> dict:
        return {
            "temp_c": self.temp_c,
            "temp_f": round(self.temp_f, 1),
            "feels_like_c": self.feels,
            "feels_like_f": round(self.feels_like_f, 1),
            "condition": self.condition,
            "wind_kph": self.wind_kph,
            "humidity": self.humidity,
            "uv_index": self.uv_index,
        }


# --------------------------------------------------------------------------- #
# target warmth: 1 (hot) .. 5 (freezing)
# --------------------------------------------------------------------------- #
def target_warmth(w: Weather) -> float:
    t = w.feels - w.wind_kph * 0.1  # crude wind-chill
    if t >= 28:
        base = 1.0
    elif t >= 23:
        base = 1.5
    elif t >= 18:
        base = 2.0
    elif t >= 12:
        base = 3.0
    elif t >= 6:
        base = 3.5
    elif t >= 0:
        base = 4.0
    else:
        base = 5.0
    if w.precipitating and base >= 3.5:
        base += 0.5  # need layers when wet + cold
    return min(base, 5.0)


def warmth_match(g: Garment, target: float) -> float:
    diff = abs(g.warmth - target)
    if diff <= 1:
        return 1.0
    return max(0.0, 1.0 - (diff - 1) * 0.4)


def formality_match(g: Garment, formality: str) -> float:
    if g.formality == "all":
        return 0.7
    gi = FORMALITY_ORDER.index(g.formality)
    fi = FORMALITY_ORDER.index(formality)
    if gi == fi:
        return 1.0
    if abs(gi - fi) == 1:
        return 0.5
    return 0.0


def occasion_match(g: Garment, occasion_tags: list[str]) -> float:
    gtags = {o.strip() for o in (g.occasions or "").split(",") if o.strip()}
    return 1.0 if (set(occasion_tags) & gtags) else 0.0


def _hue(hex_color: str) -> float | None:
    """Return HSV hue (0-360) or None if not parseable."""
    try:
        h = hex_color.lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        r, g, b = (int(h[i : i + 2], 16) / 255.0 for i in (0, 2, 4))
    except Exception:
        return None
    mx, mn = max(r, g, b), min(r, g, b)
    d = mx - mn
    if d == 0:
        return None
    if mx == r:
        deg = ((g - b) / d) % 6
    elif mx == g:
        deg = (b - r) / d + 2
    else:
        deg = (r - g) / d + 4
    return deg * 60


def harmony(a: Garment, b: Garment) -> float:
    ha, hb = _hue(a.color_hex), _hue(b.color_hex)
    if ha is None or hb is None:
        return 0.4  # neutral default
    diff = min(abs(ha - hb), 360 - abs(ha - hb))
    if diff <= 30 or diff >= 150:
        return 1.0  # analogous or complementary
    if diff <= 60:
        return 0.7
    return 0.3


def rotation_bonus(g: Garment, wear_count: int | None = None) -> float:
    # prefer items worn less / not recently
    wc = g.wear_count if wear_count is None else wear_count
    if wc <= 0:
        return 1.0
    return max(0.0, 1.0 - wc / 20.0)


PROMPT_KEYWORDS = {
    "blue": "blue", "navy": "navy", "black": "black", "white": "white",
    "light blue": "light blue", "indigo": "indigo",
    "gray": "gray", "grey": "gray", "green": "green", "red": "red",
    "brown": "brown", "tan": "tan", "beige": "tan", "pink": "pink",
    "formal": "formal", "casual": "casual", "dressy": "formal",
    "smart": "smart-casual", "wool": "wool", "cotton": "cotton",
    "denim": "denim", "leather": "leather", "comfy": "casual",
}


def prompt_bonus(g: Garment, prompt: str | None) -> float:
    if not prompt:
        return 0.0
    text = prompt.lower()
    score = 0.0
    for word, tag in PROMPT_KEYWORDS.items():
        if word in text:
            if tag in (g.color_tags or "").split(","):
                score += 1
            if tag == g.formality:
                score += 1
            if tag == g.material:
                score += 1
    return min(1.0, score / 2.0)


# --------------------------------------------------------------------------- #
# style-profile awareness (per-user personalization)                          #
# --------------------------------------------------------------------------- #
_PATTERN_WORDS = {
    "floral", "plaid", "striped", "stripes", "polka", "check", "checks",
    "gingham", "camouflage", "graphic", "print", "prints", "tartan",
    "houndstooth", "dots", "dot",
}


def _color_tags(g: Garment) -> set[str]:
    """Lowercased set of a garment's color tags (handles str or list)."""
    ct = g.color_tags
    if isinstance(ct, (list, tuple)):
        return {str(c).strip().lower() for c in ct if c}
    return {c.strip().lower() for c in (ct or "").split(",") if c.strip()}


def _apply_warmth_bias(target: float, warmth_bias) -> float:
    """A user who 'runs cold' gets a warmer target; 'runs hot' a lighter one."""
    try:
        b = int(warmth_bias or 0)
    except (TypeError, ValueError):
        return target
    if b > 0:
        target += 0.5
    elif b < 0:
        target -= 0.5
    return max(1.0, min(5.0, target))


def _guardrail_blocked(g: Garment, guardrails) -> bool:
    """True if a garment violates a hard 'never wear' guardrail"""
    name = (g.name or "").lower()
    tags = _color_tags(g)
    for gr in guardrails or []:
        gr = gr.strip().lower()
        if gr.startswith("avoid_color:"):
            c = gr[len("avoid_color:"):].strip().lower()
            if c and (c in tags or c in name):
                return True
        elif gr == "no_patterns":
            if any(p in name for p in _PATTERN_WORDS) or (tags & _PATTERN_WORDS):
                return True
        elif gr == "no_shorts":
            if "shorts" in name or (g.category == "bottom" and re.search(r"\bshort\b", name)):
                return True
        elif gr == "no_skirts":
            if "skirt" in name:
                return True
        elif gr == "no_dresses":
            if g.category == "dress" or ("dress" in name and "shoe" not in name):
                return True
        elif gr == "no_tank":
            if "tank" in name or "sleeveless" in name:
                return True
        elif gr == "no_hoodies":
            if "hoodie" in name:
                return True
        elif gr == "no_sandals":
            if "sandal" in name or "flip-flop" in name or "flip flop" in name:
                return True
        elif gr.startswith("never:"):
            token = gr[len("never:"):].strip().lower()
            if token and (token in name or token in tags):
                return True
    return False


def _formality_zone_penalty(g: Garment, zone: dict | None) -> float:
    """Penalize garments outside the user's preferred formality range."""
    if not zone:
        return 0.0
    lo, hi = zone.get("min"), zone.get("max")
    if not lo or not hi:
        return 0.0
    if lo not in FORMALITY_ORDER or hi not in FORMALITY_ORDER:
        return 0.0
    if g.formality == "all" or g.formality not in FORMALITY_ORDER:
        return 0.0
    gi = FORMALITY_ORDER.index(g.formality)
    lo_i = FORMALITY_ORDER.index(lo)
    hi_i = FORMALITY_ORDER.index(hi)
    if gi < lo_i:
        return -8.0 * (lo_i - gi)
    if gi > hi_i:
        return -8.0 * (gi - hi_i)
    return 0.0


def _palette_bonus(g: Garment, palette: dict | None) -> float:
    """Bonus for favorite colors, penalty for colors the user avoids."""
    if not palette:
        return 0.0
    tags = _color_tags(g)
    b = 0.0
    for c in palette.get("fav") or []:
        if c.strip().lower() in tags:
            b += 4.0
            break
    for c in palette.get("avoid") or []:
        if c.strip().lower() in tags:
            b -= 10.0
            break
    return b


def _occasion_weights_bonus(g: Garment, occasion_weights: dict | None) -> float:
    """Boost garments for occasions the user does a lot in a typical week."""
    if not occasion_weights:
        return 0.0
    gtags = {o.strip().lower() for o in (g.occasions or "").split(",") if o.strip()}
    b = 0.0
    for occ, count in occasion_weights.items():
        try:
            c = float(count)
        except (TypeError, ValueError):
            continue
        if occ.strip().lower() in gtags:
            b += min(max(c, 0.0), 5.0)
    return b


def _style_tags_bonus(g: Garment, style_tags: list | None) -> float:
    if not style_tags:
        return 0.0
    name = (g.name or "").lower()
    tags = _color_tags(g)
    mat = (g.material or "").lower()
    b = 0.0
    for st in style_tags:
        st = st.strip().lower()
        if not st:
            continue
        if st in name or st in tags or st == g.formality or st in mat:
            b += 2.0
    return b


def _personalized_notes(profile: dict, has_affinity: bool) -> list[str]:
    """Human-readable reasoning lines reflecting the user's profile."""
    lines: list[str] = []
    try:
        b = int(profile.get("warmth_bias") or 0)
    except (TypeError, ValueError):
        b = 0
    if b > 0:
        lines.append("you run cold — bias toward warmer layers")
    elif b < 0:
        lines.append("you run hot — bias toward lighter layers")
    if profile.get("guardrails"):
        lines.append("respecting your 'never wear' preferences")
    zone = profile.get("formality_zone") or {}
    if zone.get("min") and zone.get("max"):
        lines.append(f"staying in your {zone['min']}–{zone['max']} formality range")
    if (profile.get("palette") or {}).get("fav"):
        lines.append("prioritizing your favorite colors")
    if has_affinity:
        lines.append("tuned to your likes and dislikes")
    return lines


# --------------------------------------------------------------------------- #
# main entry
# --------------------------------------------------------------------------- #
def recommend(
    w: Weather,
    activity: str = "casual",
    prompt: str | None = None,
    wardrobe: Wardrobe | None = None,
    user_id: int = 1,
    owned_only: bool = False,
    profile: dict | None = None,
) -> dict:
    wardrobe = wardrobe or Wardrobe()
    items = wardrobe.all(user_id)
    if owned_only:
        items = [g for g in items if g.owned]

    profile = profile or {}
    guardrails = profile.get("guardrails") or []
    warmth_bias = profile.get("warmth_bias")
    formality_zone = profile.get("formality_zone") or {}
    palette = profile.get("palette") or {}
    occasion_weights = profile.get("occasion_weights") or {}
    style_tags = profile.get("style_tags") or []

    # Personalize shared clothing per viewer: a shared item this person marked
    # "doesn't fit" is never suggested to them.
    items = [
        g for g in items
        if not (g.shared and sharing.state(user_id, g.id).get("fit_ok") == 0)
    ]
    # Shared garments rotate by the VIEWER's own wear_count, not the owner's.
    shared_states = {g.id: sharing.state(user_id, g.id) for g in items if g.shared}

    # Hard guardrails from the style profile (e.g. "never yellow", "no shorts").
    if guardrails:
        items = [g for g in items if not _guardrail_blocked(g, guardrails)]

    formality, occasion_tags = ACTIVITY_MAP.get(
        activity.lower(), ACTIVITY_MAP["casual"]
    )
    target = _apply_warmth_bias(target_warmth(w), warmth_bias)
    precipitating = w.precipitating

    # Online learning signal: summed, time-decayed, context-weighted
    # likes/dislikes/ratings/saves/try-ons. The feedback loop must never break a
    # suggestion, so any DB hiccup is ignored.
    affinity: dict[int, float] = {}
    try:
        affinity = interactions.affinity_map(user_id, activity)
    except Exception:  # noqa: BLE001
        affinity = {}

    if not items:
        return {
            "outfit": {},
            "reasoning": [
                "Your wardrobe is empty — add some clothes in the Wardrobe tab first.",
            ],
            "weather_used": w.to_dict(),
            "activity": activity,
            "note": "empty_wardrobe",
        }

    # `_top` is set after the first picks but referenced inside `best()`/`score()`
    # for color harmony — initialize it so those early calls see None.
    _top: Garment | None = None
    # Mutable warmth target: lowered when we'll layer (a lighter top under a
    # jacket) and reset before the outer layers / footwear are scored.
    _target = target

    def viewer_wear_count(g: Garment) -> int:
        if g.shared:
            return int((shared_states.get(g.id) or {}).get("wear_count") or 0)
        return g.wear_count

    def score(g: Garment, top: Garment | None = None) -> float:
        s = (
            40.0 * warmth_match(g, _target)
            + 20.0 * formality_match(g, formality)
            + 10.0 * occasion_match(g, occasion_tags)
            + 6.0 * rotation_bonus(g, viewer_wear_count(g))
            + 5.0 * prompt_bonus(g, prompt)
            + _formality_zone_penalty(g, formality_zone)
            + _palette_bonus(g, palette)
            + _occasion_weights_bonus(g, occasion_weights)
            + _style_tags_bonus(g, style_tags)
        )
        aff = affinity.get(g.id, 0.0)
        if aff:
            s += max(-10.0, min(10.0, aff))
        if precipitating:
            if g.waterproof:
                s += 10.0
            elif g.category in ("outerwear", "top", "dress", "swimsuit"):
                s -= 15.0
        if top and g.category in ("bottom", "outerwear", "footwear", "accessory"):
            s += 8.0 * harmony(top, g)
        return s

    def best(category: str, exclude: set[int] | None = None, require: int | None = None) -> Garment | None:
        exclude = exclude or set()
        candidates = [
            g for g in items
            if g.category == category and g.id not in exclude
            and (require is None or g.waterproof == require)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda g: score(g, top=_top))

    # Decide outerwear FIRST so the top can layer under it — on a cold/wet day
    # with a jacket available we prefer a LIGHTER top (the jacket adds warmth).
    needs_outer = target >= 4.0 or precipitating
    outer_candidates = [
        g for g in items
        if g.category == "outerwear" and (not precipitating or g.waterproof == 1)
    ]
    if needs_outer and outer_candidates:
        _target = max(1.0, target - 0.7)

    # 1. top (a dress or swimsuit can fill the top slot; if chosen, bottom is optional)
    top = best("top")
    dress = best("dress")
    swimsuit = best("swimsuit")
    _top = max((g for g in (top, dress, swimsuit) if g), key=lambda g: score(g)) \
        if (top or dress or swimsuit) else None

    # 2. bottom (skip if we picked a dress/swimsuit — it covers both)
    bottom = None if (_top and _top.category in ("dress", "swimsuit")) else best("bottom")

    # back to the real target for the outer layer, footwear and accessories
    _target = target
    outerwear = best("outerwear", require=(1 if precipitating else None)) if needs_outer else None

    # 4. footwear
    footwear = best("footwear")

    # 5. accessory — cold -> wool/cotton (beanie/scarf); hot+sun -> sun hat; else belt
    accessory: Garment | None = None
    accs = [g for g in items if g.category == "accessory"]
    if target >= 4.0:
        cold_accs = [a for a in accs if a.material in ("wool", "cotton")]
        if cold_accs:
            accessory = max(cold_accs, key=lambda a: score(a, top=_top))
    elif w.uv_index >= 7 or w.feels >= 28:
        accessory = next((a for a in accs if "sun hat" in a.name.lower()), None)
    else:
        accessory = next((a for a in accs if "belt" in a.name.lower()), None)

    def ser(g: Garment | None) -> dict | None:
        return g.to_dict() if g else None

    personal = _personalized_notes(profile, bool(affinity))
    return {
        "outfit": {
            "top": ser(_top),
            "bottom": ser(bottom),
            "outerwear": ser(outerwear),
            "footwear": ser(footwear),
            "accessories": [ser(a) for a in [accessory] if a],
        },
        "reasoning": _build_reasoning(
            w, target, formality, precipitating, _top, bottom, outerwear, prompt,
            personal=personal,
        ),
        "weather_used": {
            "temp_c": w.temp_c,
            "temp_f": round(w.temp_f, 1),
            "feels_like_c": w.feels,
            "feels_like_f": round(w.feels_like_f, 1),
            "condition": w.condition,
            "wind_kph": w.wind_kph,
            "uv_index": w.uv_index,
        },
        "activity": activity,
        "personalized": bool(personal),
    }


def _build_reasoning(
    w: Weather, target: float, formality: str, precipitating: bool,
    top: Garment | None, bottom: Garment | None, outerwear: Garment | None,
    prompt: str | None, personal: list[str] | None = None,
) -> list[str]:
    lines: list[str] = []
    if precipitating:
        lines.append(f"{w.condition.title()} outside → picking waterproof layer")
    lines.append(f"{w.feels:.0f}°C feels → target warmth {target:.0f}/5")
    lines.append(f"{formality} activity → matching formality level")
    if top and bottom and top.category not in ("dress", "swimsuit"):
        lines.append(f"{top.name} + {bottom.name} are color-compatible")
    if top and top.category in ("dress", "swimsuit"):
        lines.append(f"{top.name} is a one-piece — no separate bottom needed")
    if outerwear:
        lines.append(f"layering with {outerwear.name}")
    if prompt:
        lines.append(f"style prompt '{prompt}' factored in")
    lines.extend(personal or [])
    return lines
