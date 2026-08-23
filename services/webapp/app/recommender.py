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

from dataclasses import dataclass

from .wardrobe import Garment, Wardrobe
from . import interactions, sharing

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


def rotation_bonus(g: Garment) -> float:
    # prefer items worn less / not recently
    if g.wear_count <= 0:
        return 1.0
    return max(0.0, 1.0 - g.wear_count / 20.0)


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
# main entry
# --------------------------------------------------------------------------- #
def recommend(
    w: Weather,
    activity: str = "casual",
    prompt: str | None = None,
    wardrobe: Wardrobe | None = None,
    user_id: int = 1,
    owned_only: bool = False,
) -> dict:
    wardrobe = wardrobe or Wardrobe()
    items = wardrobe.all(user_id)
    if owned_only:
        items = [g for g in items if g.owned]
    # a shared family garment the viewer has marked "doesn't fit" is never suggested
    items = [
        g for g in items
        if not (g.shared and sharing.state(user_id, g.id).get("fit_ok") == 0)
    ]
    # rec-engine L2/3: per-user style + ALS bonuses (no-op when no data exists)
    from . import personalize
    pers = personalize.Personalizer(user_id, items)
    formality, occasion_tags = ACTIVITY_MAP.get(
        activity.lower(), ACTIVITY_MAP["casual"]
    )
    target = target_warmth(w)
    precipitating = w.precipitating

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
    # mutable warmth target: dropped ~0.7 when we'll layer (a lighter top under
    # a jacket) and reset before the outer layers / footwear are scored
    _target = target

    def score(g: Garment, top: Garment | None = None) -> float:
        s = (
            40.0 * warmth_match(g, _target)
            + 20.0 * formality_match(g, formality)
            + 10.0 * occasion_match(g, occasion_tags)
            + 6.0 * rotation_bonus(g)
            + 5.0 * prompt_bonus(g, prompt)
        )
        if precipitating:
            if g.waterproof:
                s += 10.0
            elif g.category in ("outerwear", "top", "dress", "swimsuit"):
                s -= 15.0
        if top and g.category in ("bottom", "outerwear", "footwear", "accessory"):
            s += 8.0 * harmony(top, g)
        return pers.add_to_score(g.id, s)

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

    outfit = {
        "top": ser(_top),
        "bottom": ser(bottom),
        "outerwear": ser(outerwear),
        "footwear": ser(footwear),
        "accessories": [ser(a) for a in [accessory] if a],
    }
    # log "shown" interactions so the engine can learn from recommendations
    interactions.log_outfit_shown(user_id, outfit, {"activity": activity, "prompt": prompt})

    reasoning = _build_reasoning(
        w, target, formality, precipitating, _top, bottom, outerwear, prompt
    )
    if pers.active:
        reasoning.append("personalized to your style + wear history")

    return {
        "outfit": outfit,
        "reasoning": reasoning,
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
    }


def _build_reasoning(
    w: Weather, target: float, formality: str, precipitating: bool,
    top: Garment | None, bottom: Garment | None, outerwear: Garment | None,
    prompt: str | None,
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
    return lines
