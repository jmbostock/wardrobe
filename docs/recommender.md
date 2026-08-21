# altacloset — Wardrobe Schema & Recommender Spec

The MVP recommender is **deterministic rule-based scoring** — no GPU, <10ms, fully
explainable. An LLM (Phase 3) only *explains* the pick in natural language.

## 1. Wardrobe schema (sqlite, `data/db/altacloset.db`)

Multi-user: every garment belongs to a `user_id`; each user gets their own seed
wardrobe copy. Auth (`users`, `sessions`) lives in the same db.

```sql
CREATE TABLE users (
    id             INTEGER PRIMARY KEY,
    username       TEXT NOT NULL UNIQUE,
    password_salt  TEXT NOT NULL,
    password_hash  TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL
);

CREATE TABLE garments (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,              -- "Navy merino crewneck"
    category      TEXT NOT NULL,              -- top|bottom|dress|outerwear|footwear|accessory
    color_hex     TEXT,                       -- for harmony + rendering
    color_tags    TEXT,                       -- comma list e.g. "navy,dark"
    warmth        INTEGER NOT NULL DEFAULT 3, -- 1 (thin/cool) .. 5 (heavy/warm)
    waterproof    INTEGER NOT NULL DEFAULT 0, -- 1 = rain-safe
    formality     TEXT NOT NULL DEFAULT 'casual', -- casual|smart-casual|business|formal
    occasions     TEXT,                       -- comma list: office,date,hiking,event,...
    material      TEXT,
    image_path    TEXT NOT NULL,              -- data/wardrobe/<user_id>/<id>.png (try-on)
    fit           TEXT DEFAULT 'regular',     -- slim|regular|loose
    last_worn     TEXT,                       -- ISO date, for rotation
    wear_count    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_garments_user ON garments(user_id);

CREATE TABLE outfits (               -- phase 4
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    label      TEXT,                          -- e.g. "Monday office"
    garment_ids TEXT,                         -- JSON array, order = layering
    created_at TEXT
);
```

### Seed wardrobe (`services/webapp/app/wardrobe.py::seed`)
Ship ~20–30 garments across categories so the MVP is demoable immediately:
tops (tees, button-downs, knits, hoodies), bottoms (jeans, chinos, shorts),
outerwear (rain shell, packable puffer, denim jacket), footwear, accessories (hat, scarf).

## 2. Inputs

```python
@dataclass
class Weather:
    temp_c: float; feels_like_c: float; condition: str  # clear|cloudy|rain|snow|wind|fog
    wind_kph: float; humidity: int; uv_index: float

activity: str    # free text, matched against a formality/occasion map
prompt: str | None  # free-form style hint (parsed for color/occasion keywords, later LLM)
```

`activity → (formality_level, occasion_tags)` lookup table:

| Activity (examples) | formality | occasion tags |
|---|---|---|
| office / interview / work | business | office |
| date / dinner / night out | smart-casual | date,event |
| errands / casual / home | casual | casual |
| hiking / gym / beach | casual | active |
| wedding / gala / formal | formal | event |

## 3. Scoring

For each garment compute a weighted score; pick the best non-conflicting combination
(one top, one bottom, optional outerwear, footwear, accessory).

```
target_warmth = clamp( map(feels_like, hot=1 .. cold=5), 1, 5 )
                + (2 if snow/rain and cold)      # need layers

score(g) =
    + 40 * warmth_match(g, target)         # 1 if g.warmth within ±1 of target, decays after
    + 20 * formality_match(g, activity)    # exact 1, adjacent 0.5, else 0
    + 10 * occasion_match(g, activity)     # has tag
    + 10 * waterproof_bonus(g)             # 1 if raining/snowing and g.waterproof
    - 15 * waterproof_penalty(g)           # if raining and NOT waterproof and is outerwear/top
    +  8 * harmony(top, bottom)            # color-wheel compat (analogous/complementary)
    +  6 * rotation_bonus(g)               # not worn in N days / low wear_count
    +  5 * prompt_bonus(g)                 # prompt keywords match color/material/formality
```

Layering rules:
- `target_warmth >= 4` → require outerwear layer.
- `temp < 0` → prefer knit/wool materials, add hat/scarf.
- `temp >= 28 or uv_index >= 7` → cap/sun protection, light fabrics.
- Raining → outerwear must be `waterproof=1`; forbid suede/denim outer.

## 4. Output

```json
{
  "outfit": {
    "top": {"id": 3, "name": "Navy merino crewneck", "image_path": "...", "color_hex": "#1f2a44"},
    "bottom": {"id": 12, "name": "Dark slim chinos", ...},
    "outerwear": {"id": 8, "name": "Packable puffer", ...},
    "footwear": {"id": 17, "name": "Low-top sneakers", ...},
    "accessories": [{"id": 19, "name": "Wool beanie", ...}]
  },
  "reasoning": [
    "13°C and drizzle → light rain shell (waterproof) + warm knit",
    "office activity → business-formal, dark chinos match",
    "navy + dark gray are analogous colors → harmonious"
  ],
  "scores": {"top": 0.83, "bottom": 0.71, ...},
  "weather_used": {"temp_c": 13.0, "condition": "rain"}
}
```

## 5. Testing the recommender (no GPU needed)

- Unit tests: cold/rain/formal/hot cases assert expected category selections.
- A quick CLI: `python -m app.recommender --activity office --temp 13 --condition rain`.
- Golden-image the reasoning strings so changes are reviewable.
