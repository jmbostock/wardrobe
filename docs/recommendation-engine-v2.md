# Recommendation Engine v2 — Plan (shared wardrobe, learning, ~300 items/person)

> Status: **PLANNED** (2026-08-23). No code changed yet.
> Goal: an **accurate, self-improving** outfit recommender for **a few people** with
> **~300 garments each**, where some garments are **shared between people**, that
> **learns from every recommendation** and keeps learning as the wardrobe grows.
> Bias: **use existing open-source fashion models/repos** and keep the pipeline
> **streamlined** — no research-grade overkill, no big-data infra.

---

## 0. TL;DR

Keep the existing deterministic rule-based scorer as the cold-start baseline, then
add **two cheap, battle-tested ML layers** on top, fueled by a new **interaction log**:

```
┌────────────────────────────────────────────────────────────────────┐
│                     /api/recommend  (unchanged contract)           │
│   final score = α·rules + β·FashionCLIP style + γ·ALS learning     │
└───────┬──────────────────────────────┬─────────────────────────────┘
        │                              │
   ┌────▼─────┐                 ┌──────▼───────┐
   │ Layer 1  │                 │  Layer 2     │        Layer 3
   │ Rules    │   FashionCLIP   │  embeddings  │   implicit (ALS)
   │ (exists) │ ──────────────► │  user-style  │ ─► collaborative,
   │ + enh    │   embed images  │  centroid    │    partial-fit learns
   └────┬─────┘                 └──────┬───────┘    from interactions
        │                              │
        └─────────── interaction log (feedback) ────────┘
                    every event: shown, tried-on, worn,
                    saved, rated, disliked
```

- **Layer 1 — rules** (`recommender.py`, already live): weather/formality/occasion/
  waterproof/color-harmony/rotation + the already-designed enhancements (rating,
  recency, last-worn, pair-history from rated outfits). Handles cold start, <10 ms.
- **Layer 2 — FashionCLIP** (`patrickjohncyh/fashion-clip`, `pip install fashion-clip`):
  512-d embeddings per garment photo. Builds a per-user **style centroid** from what
  they wear/rate/save — this is the "gets to know your taste" layer, and it
  personalizes instantly with zero training. Also powers text search + visual dup.
- **Layer 3 — ALS** (`benfred/implicit`, `pip install implicit`): collaborative
  filter trained on the interaction log (wear / try-on / save / rating / dislike).
  `partial_fit_items()` ingests new garments without a full retrain. This is the
  "learns from everyone's history, including shared items" layer.
- **Event log** (new `interactions` table) is the learning fuel for Layers 2 & 3
  and the source of truth for measuring accuracy over time.
- **Sharing** = `garment_access(user_id, garment_id, role)` join table + a
  `user_garment_state` table for per-person wear/rating/fit on shared items.
- **Batch pipeline** = one script/endpoint: ingest → vision tag-read (extended) →
  background-remove → embed → dedup → QC → `partial_fit` → live. ~minutes per 300.

---

## 1. Evaluation of the current system (why this plan)

### What's already good (keep)
- Deterministic, explainable rule scorer; <10 ms, CPU-only, live in `/api/recommend` + `/api/suggest`.
- Rich garment metadata captured at intake: category, `color_hex`/`color_tags`,
  `warmth`, `waterproof`, `formality`, `occasions`, `material`, `fit`, `brand`, `sizes`,
  `rating`, `owned`, `created_at`, `last_worn`, `wear_count`.
- Vision tag-read on upload (llama.cpp Qwen2.5-VL on 202/187), `parse-link` product
  scraping, dHash near-dup detection, image-quality QC, saved outfits with ratings.
- LLM stylist chat (DeepSeek) already provides the conversational layer.

### What's missing for this goal (gaps this plan closes)
| Gap | Why it matters here | Fix |
|---|---|---|
| **No feedback/event log** | Can't "learn from recommendations" without recording what was shown vs. what was acted on | new `interactions` table |
| **Scorer ignores 6+ stored signals** | rating, `created_at`, `last_worn`, `brand`, `fit`, outfit ratings/pairs are unused (the Phase-3 design doc's Layer 1 was never implemented) | implement `rating_bonus`, `recency_bonus`, `last_worn_bonus`, `pair_history_bonus` |
| **No per-user taste model** | every user gets identical rule output; cold personalization absent | FashionCLIP style centroid |
| **No collaborative learning** | can't exploit "people who like X also save Y", esp. across shared items | ALS on interaction log |
| **Ownership is 1:1** (`garments.user_id`) | shared clothes unsupported | `garment_access` + `user_garment_state` |
| **Dup detection is photo-only (dHash)** | "another black tee I forgot I owned" isn't caught; no style similarity | add embedding-distance dup + style similarity |
| **No offline eval** | can't prove it's "accurate and learning" | time-split HitRate@10 / NDCG + wear-conversion |

---

## 2. Scale assumptions (design targets)

- **Users:** 3–6 people (household/friends). Per-user wardrobe **~300 owned** garments,
  plus a shared pool. Total catalog **~900–1,800 garments** in one `garments` table.
- **Interactions:** a few dozen–few hundred events per user per month. Tiny by ML
  standards — this is why **ALS + embeddings, not deep sequential models**, win here.
- **Compute:** webapp on 187 (CPU-only) or 202 (has GPU via tunnel). FashionCLIP
  embedding of 1,800 images ≈ 5–10 min on CPU, <2 min on GPU. ALS fit on this size ≈
  **seconds**. Everything can run nightly + on-demand after each batch.

> Because the numbers are this small, "streamlined" = a hybrid of (a) rules,
> (b) embedding similarity, (c) ALS — all well-understood, CPU-friendly, no
> training cluster, no big-data stack.

---

## 3. Data model changes

### 3.1 New: `interactions` (the learning log — most important table)

```sql
CREATE TABLE IF NOT EXISTS interactions (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    garment_id  INTEGER NOT NULL REFERENCES garments(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,   -- shown|tried_on|saved|worn|rated_up|rated_down|disliked|searched|chat_swap
    weight      REAL NOT NULL DEFAULT 1.0,  -- confidence for ALS (see §5)
    context     TEXT NOT NULL DEFAULT '{}', -- JSON: {activity, weather, prompt, outfit_id}
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_interactions_user  ON interactions(user_id);
CREATE INDEX IF NOT EXISTS idx_interactions_garment ON interactions(garment_id);
CREATE INDEX IF NOT EXISTS idx_interactions_time  ON interactions(created_at);
```

Every event the UI already generates gets logged here:
- `/api/recommend` & `/api/suggest` → `shown` (one per garment in the outfit)
- `/api/tryon*` → `tried_on`
- `/api/outfits` POST (save a look) → `saved` for each garment in the outfit
- `garment.last_worn` updated / "Did you wear it?" = yes → `worn`
- garment/outfit rating set to 7–10 → `rated_up`; ≤3 → `rated_down`
- new **Dislike** button on a recommended card → `disliked`
- stylist chat "Try it →" swap → `chat_swap`

This one table feeds the style centroid, the ALS matrix, and offline eval.

### 3.2 New: `garment_access` + `user_garment_state` (sharing)

```sql
CREATE TABLE IF NOT EXISTS groups (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE                 -- 'Family' is auto-seeded for every user
);
CREATE TABLE IF NOT EXISTS group_members (
    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (group_id, user_id)
);
-- garments gain: share_group_id INTEGER REFERENCES groups(id)  (NULL = private to owner)
-- added via db._migrate() (one-shot ALTER, guarded by PRAGMA table_info check).

CREATE TABLE IF NOT EXISTS user_garment_state (
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    garment_id  INTEGER NOT NULL REFERENCES garments(id) ON DELETE CASCADE,
    wear_count  INTEGER NOT NULL DEFAULT 0,   -- per-user (shared clothes rotate per person)
    last_worn   TEXT,                          -- per-user
    rating      INTEGER NOT NULL DEFAULT 0,    -- per-user 0..10
    fit_ok      INTEGER,                        -- NULL unknown, 1 fits, 0 doesn't (critical for sharing)
    PRIMARY KEY (user_id, garment_id)
);
```

- `garments.user_id` stays = **owner** (backward compatible). Visibility for others =
  one `share_group_id` column on the garment → **whole family group** (flat model,
  matches "mostly family group"). No per-person grant rows to manage.
- "My wardrobe" query = `garments` where owner OR `share_group_id` in the user's groups.
- **Recommendation candidate set** for user X = owned ∪ family-shared, **filtered by
  `fit_ok != 0`** (a shared jacket that's too small for X never gets suggested to X).
- `user_garment_state` overrides the aggregate `garments.*` counters **per person**,
  so "who wore the shared coat last" and "whose rotation is due" are correct.
- **Privacy default:** sharing is opt-in per garment (owner toggles "shared to Family");
  wishlist/owned=0 items are never shared.

### 3.3 New: `garment_embeddings` (derived, not hand-entered)

```sql
CREATE TABLE IF NOT EXISTS garment_embeddings (
    garment_id INTEGER PRIMARY KEY REFERENCES garments(id) ON DELETE CASCADE,
    model      TEXT NOT NULL DEFAULT 'fashion-clip-v2',  -- so we can re-embed on model bump
    dim        INTEGER NOT NULL DEFAULT 512,
    vector     BLOB NOT NULL,        -- float32 array, sqlite3 serialize
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

Computed at ingest (see §7), refreshed when the embedding model changes. Used for:
style-centroid similarity, "find similar", text→garment retrieval, and visual-dup.

### 3.4 Extended garment metadata (intake)

Add **style attributes** the vision model extracts alongside brand/color/category/sizes
(current `aifill` already does the single-pass tag-read; just extend the prompt):

| Field | Where | Why it makes recommendations accurate |
|---|---|---|
| `pattern` | garments | solid / striped / plaid / floral / graphic — pattern-mixing rules (never 2 loud prints) |
| `silhouette` | garments | fitted / relaxed / oversized — pairing (oversized top ↔ slim bottom) |
| `sleeve_length` | garments | short / 3-quarter / long — season + formality |
| `neckline` | garments | crew / v-neck / collar / turtleneck / strapless — layering & dress rules |
| `hem_length` | garments (dresses/tops) | crop / waist / hip / knee / midi / maxi — proportions |
| `season` | garments | summer / transitional / winter — adds to warmth target |
| `style_keywords` | garments | preppy, minimal, athleisure, boho, street, classic… — centroid + compatibility |
| `color_hex` (auto) | garments | dominant-color extractor when the user didn't set it (extends existing palette) |

Also: keep `fit` (slim/regular/loose) — it's captured but **currently unused by the
scorer**; wire it into harmony/proportion rules.

> Do **not** over-schema this. These are free-text/small-vocab tags the vision model
> outputs in one pass; the rule scorer uses them as bonus signals, the embedding
> already encodes most of this visually. The metadata makes the **explainable**
> "why" better and fixes cases the embedding can't see (e.g., "this is silk").

### 3.5 Per-individual profile (users) — what to capture, and why

Sharing clothes across a family means the **person** matters as much as the **garment**.
Everything below is **optional** and most of it is *learned* from feedback over time;
the profile just accelerates cold start and encodes hard guardrails learning can't fix
(e.g., "it doesn't fit me"). Collected once at account setup, editable later.

| Attribute | Needed? | What it buys the recommender |
|---|---|---|
| **Sex / gender** (m/f/…) | Recommended (optional) | biases category fit & style priors for cold start (e.g., fit expectations on tops/dresses) |
| **Height** | Recommended | proportions (cropped vs. full-length hems) |
| **Sizes** per category (S/M/L/XL, waist×inseam) + **shoe size** | Recommended (sharing-critical) | auto-suggests `fit_ok` on shared garments so a too-small coat is never suggested |
| **Warmth bias** (runs cold −1 / runs hot +1) | Recommended | offsets the global weather `target_warmth` per person |
| **Formality comfort zone** (min–max) | Recommended | hard filter: "never formal" removes gala/formal candidates |
| **Never-wear list** (no shorts / no patterns / no yellow) | Nice | strong cold-start guardrail; instant filter |
| **Style keywords** (minimal / preppy / athleisure …) | Nice | seeds the FashionCLIP style centroid before feedback exists |
| **Occasion schedule** (office 5×/wk, gym 3×/wk) | Nice | weights occasion matching to what they actually do |
| **Favorite palette / colors to avoid** | Optional | harmony prior + centroid color bias |
| **Age range** | Optional (sensitive) | subtle styling priors only; not required |

**Recommendation:** collect at least **sex, height, sizes (incl. shoe), warmth bias,
formality zone, never-wear** at setup — ~2 minutes of input, removes most cold-start
blindness. Everything else gets learned.

---

## 4. Layer 2 — FashionCLIP embeddings & per-user style (the personalization layer)

**Model:** `patrickjohncyh/fashion-clip` (FashionCLIP 2.0, ViT-B/32, fine-tuned on
~800K Farfetch products; ~0.83 weighted F1 on fashion benchmarks vs 0.66 for OpenAI
CLIP). Handles flat-lay product images (the exact input we store). `pip install fashion-clip`.

### 4.1 Embedding & storage
- At ingest (and on-demand backfill), run each garment image → 512-d vector → `garment_embeddings`.
- CPU throughput ~3–6 img/s; GPU much faster. Run in the batch pipeline, not per-request.

### 4.2 User style centroid (the "learns my taste" part)
```python
def user_style_vector(user_id, embeddings, interactions, decay_days=90):
    """Weighted mean of FashionCLIP embeddings of garments the user engaged with.
    Weights: worn=4, saved=3, tried_on=2, rated_up=2·(r-6), disliked=-3.
    Older interactions decay exponentially (half-life ~decay_days)."""
    ...
```
- **Cold start (0–5 events):** centroid = owner's + any shared-item history; else 0 → rules dominate.
- **Warm:** candidate garment score `+= β · cos(embed_g, centroid)`.
- This is how the system "starts knowing" a new person's taste almost immediately and
  keeps moving as they wear/save/rate — **no training, updates are O(catalog) math.**

### 4.3 Text retrieval & chat grounding
- Stylist (Cher/DeepSeek) can call `search(query, user_id)` → top-k by
  `cos(embed_g, text_embed(query))`, letting it answer "do I have a white linen shirt?"
  with real candidates instead of hallucinating.
- "Show me similar to this" button = top-k nearest embeddings (minus self) in user's pool.

### 4.4 Compatibility beyond color
- Rule harmony already handles color-wheel. FashionCLIP adds **style/pattern/silhouette**
  compatibility: e.g., a striped top + plaid bottom gets a low style-compat score, a
  silk blouse + tailored trouser a high one. Concretely: score top+bottom via
  `cos(top_emb, text("pairs well with " + bottom_style_desc))` OR simpler — the user
  centroid + embedding proximity already pulls compatible items together. Keep it
  cheap: **pair compat = 0.5·(color harmony) + 0.5·cos(top_emb, bottom_emb)**, tuned later.

---

## 5. Layer 3 — ALS collaborative learning (`benfred/implicit`)

**Why ALS here:** it's the standard, tiny-footprint way to learn "who likes what"
from implicit signals; `partial_fit_items()` handles **new batches without retraining**;
`similar_items()` gives outfit companions; `explain()` gives per-item reasons
(which feed the "why" UI). All CPU, seconds to fit at our scale.

```python
from implicit.als import AlternatingLeastSquares

model = AlternatingLeastSquares(factors=64, regularization=0.05,
                                alpha=40, iterations=30, random_state=7)
model.fit(user_item_csr)                       # nightly
model.partial_fit_items(new_ids, new_item_rows) # after each ingest batch
```

### 5.1 Interaction → confidence matrix
Rows = users, cols = all garments (shared pool — even ones a user can't see, so the
model learns item structure; **filter by access at serve time**).

| Signal | confidence | notes |
|---|---|---|
| `worn` (or "did you wear it" yes) | +4 | strongest positive |
| `saved` (outfit) | +3 | second strongest |
| `tried_on` | +2 | |
| `rated_up` (7–10) | +2·(rating−6) | up to +8 |
| `chat_swap` | +1.5 | swapped-in item |
| `rated_down` (≤3) | −2 | negative confidence |
| `disliked` | −3 | strongest negative |
| `shown`, no action | 0 | implicit negative (ALS default confidence) |

### 5.2 Cold-start for new garments
- New items are seeded into the matrix (zero row) then `partial_fit_items` → factors
  start near their FashionCLIP embedding (project 512-d → 64-d via the ALS item-factor
  covariance), so a brand-new item can already rank by **visual/style** before anyone
  has interacted with it. This is the "new batch of clothes works immediately" trick.

### 5.3 Fusion with rules & style
```python
score(g) = α · rule_score(g)                     # Layer 1, existing weights
         + β · cos(embed_g, user_style_vec(g))   # Layer 2
         + γ · als_score(user, g)                # Layer 3 (0 until ≥ ~10 interactions)
```
- Start: `α=0.6, β=0.25, γ=0.15` for warm users; cold users get `α=0.75, β=0.25, γ=0`.
- Blend the **top-down rule pick** (best top + best bottom) with the **data-driven
  pick** (ALS/embedding top pair), then re-rank. Keep the outfit slots + layering
  logic in Layer 1 so weather/occasion constraints are never violated.
- α/β/γ get tuned later **on held-out interactions** (§8) — not by hand-waving.

---

## 6. Sharing semantics for recommendations

- **Family group (default):** every user belongs to the auto-seeded **"Family"** group.
  A garment is `private` (owner only) or `share_group_id = Family`. Everyone in the
  group sees shared items — no per-person granting to manage.
- Candidate pool per user = owned ∪ family-shared (with `fit_ok != 0`).
- `user_garment_state` gives per-person `wear_count`/`last_worn`/`rating` → rotation
  and pair-history are computed per person even for shared garments.
- Interactions on a shared garment update **both** the acting user's centroid/ALS
  row and (weighted 0.5) the owner's — so a great shared item benefits everyone.
- UI: garment card shows owner badge + "shared to Family" + per-user fit toggle
  ("Fits me ✓ / Doesn't fit ✗") which drives `fit_ok` (auto-suggested from sizes).

---

## 7. Batch pipeline — every time a new batch of clothes arrives

One command (`scripts/ingest-batch.sh <dir>` → `POST /api/wardrobe/batch` → background job):

```
1. INGEST      drop N photos (+ optional product URLs) → queue
2. NORMALIZE   EXIF righting + portrait guarantee (exists) + rembg background removal
3. TAG-READ    one llama.cpp Qwen2.5-VL call per image: brand, color, category, sizes,
               pattern, silhouette, sleeve_length, neckline, hem, season, style_keywords
               (extend existing single-pass aifill prompt; temperature=0)
4. EMBED       FashionCLIP → 512-d → garment_embeddings (CPU ~1–2 min per 300)
5. DEDUP       dHash (exists) OR cos(embed) ≥ 0.95 + same category → flag "similar to X"
               (embedding catches "another black tee" dHash misses)
6. QC          image-quality score (exists); flag missing warmth/formality/occasions
               (defaults applied) → surfaced for a human to confirm
7. LEARN       ALS.partial_fit_items(new_ids) + rebuild ANN index + refresh centroids
               that reference changed garments
8. COMMIT      garments visible; interactions start logging; Slack/email "N items ready"
```

**Idempotent + restartable** (per-garment state in the DB), so a 300-item import can
be paused/resumed. The whole pipeline for 300 garments is on the order of **10–20 min**
on CPU, dominated by embedding + vision tag-read, run unattended.

---

## 8. "Does it actually learn?" — offline evaluation

Because we log every `interactions` row with a timestamp, accuracy is measurable:

- **Time-split eval (nightly/weekly):** train Layers 2+3 on interactions before date T;
  for each user, hold out garments they `worn`/`saved` after T; measure
  **HitRate@10** and **NDCG@10** of the recommender vs. that held-out set.
- **Baselines to beat:** MostPopular, rules-only, rules+style (no ALS) → proves each
  layer adds value and that the system is genuinely improving over time.
- **Wear-conversion:** % of `shown` garments that become `worn`/`saved` within 7 days
  (by user) — the product metric the user actually feels.
- **A/B-lite:** "learned" ordering vs. pure rules in production, both logged; compare
  conversion. All metrics written **silently** to the DB for a future dashboard.

Only ship a layer when its eval beats the baseline on the user's own data.

---

## 9. Existing repos/models to use (and what to skip)

### Use (streamlined, proven, CPU-friendly)
| Asset | Repo/package | Role |
|---|---|---|
| **FashionCLIP 2.0** | `patrickjohncyh/fashion-clip` (HF + `pip install fashion-clip`) | embeddings, style centroid, text retrieval, visual dup, style-compat |
| **implicit** (ALS/BPR/LMF) | `benfred/implicit` | collaborative learning, `partial_fit` for new batches, `explain()` |
| **rembg** | `danielgatis/rembg` | background removal for flat-lays (better CatVTON + embeddings) |
| Vision tag-read | existing llama.cpp Qwen2.5-VL service (187) | metadata extraction (extend prompt) |
| Rule scorer + dHash + image-quality | existing altacloset code | baseline, explainability, QC |

### Skip (not worth it at this scale)
| Candidate | Why skip |
|---|---|
| **ComiRec** (THUDM, KDD'20) | TF1.x + Faiss-GPU, needs millions of sequential interactions; no sharing; research-grade glue |
| **VSCN / Type-aware compat / OutfitTransformer / FashionBERT** (Polyvore) | academic; need pre-training on Polyvore-sized data we don't have; overkill for ~1,800 items |
| **Fine-tuning FashionCLIP from scratch** | needs large labeled fashion data; only worth revisiting as a *later* few-shot fine-tune on the user's own rated outfits |

---

## 10. Implementation roadmap (streamlined, ~3 increments)

### Step A — Feedback substrate (prereq for everything) — small
- Add `interactions`, `groups`, `group_members`, `user_garment_state`,
  `garment_embeddings` + `garments.share_group_id` to `db.py._migrate()`.
- Log `shown/tried_on/saved/worn/rated/rated_down/disliked/chat_swap` from existing
  endpoints (a few lines each) — **silent, no dashboard**.
- Feedback UI: **Like / Dislike** on recommendation cards, **"Did you wear it?"**
  prompt, per-user **fit toggle** ("Fits me ✓ / Doesn't fit ✗") on shared garments,
  and a "shared to Family" toggle on the garment detail card.
- User profile fields (§3.5) collected at account setup (sex, height, sizes, warmth
  bias, formality zone, never-wear).
- **Exit:** every recommendation is logged; family sharing + per-person fit works;
  cold-start profiles exist.

### Step B — Layer 1 enhancements + Layer 2 embeddings (biggest accuracy win / cost)
- Implement `rating_bonus`, `recency_bonus`, `last_worn_bonus`, `pair_history_bonus`
  (the Phase-3 design doc spec, ~80 lines — already written).
- FashionCLIP embedder service; backfill 512-d vectors for existing wardrobe;
  user style centroid; add β·cos to `score()`; "similar to" + chat search.
- Extend the vision tag-read prompt with the §3.4 style fields; rembg step.
- **Exit:** personalization visible; recommend cards show "matches your style" reasons.

### Step C — Layer 3 ALS + eval + batch pipeline
- `implicit` ALS training job (nightly + after batches); `partial_fit_items` on ingest;
  γ·als_score in fusion; `explain()` reasons.
- `scripts/ingest-batch.sh` + `/api/wardrobe/batch` orchestrating §7.
- Offline eval (HitRate/NDCG, wear-conversion) + `/admin/metrics`; tune α/β/γ on it.
- **Exit:** dashboard proves the model beats rules-only on the user's own data.

**Sequencing rationale:** A (logging) unlocks B & C; B gives the biggest perceived
accuracy jump with the least complexity; C adds the "keeps getting better" learning
loop + the batch workflow that makes 300-item updates painless.

---

## 11. Risks & caveats

| Risk | Mitigation |
|---|---|
| **Few users** → ALS may overfit / be unstable | ALS only gets weight after ≥~10 interactions/user; centroids + rules carry cold start; eval gates each layer |
| **FashionCLIP is English/e-commerce-biased** | fine for a personal closet; re-embed on model bump; keep rules as explainable fallback |
| **Shared-item signal leakage** (my wear influences your recs) | weight shared interactions 0.5 to the non-acting user; `fit_ok` filter; privacy opt-in |
| **Embedding drift across model versions** | `garment_embeddings.model` column; one-shot re-embed job on bump |
| **Metadata errors from vision model** | single-pass prompt, temperature=0, human-confirm step in batch QC |
| **GPU contention on 202** | embedding/tag-read are CPU-ok; schedule batch off-peak; ALS is CPU seconds |

---

## 12. Decisions locked (2026-08-23)

| Question | Decision |
|---|---|
| Sharing model | **Flat "Family" group** — garment owner toggles "shared to Family"; everyone in the group sees it |
| Per-person fit data | Collect **sizes at setup** (§3.5) → auto-suggest `fit_ok`; plus manual "Fits me ✓/✗" toggle |
| Feedback UX | **Collect everything relevant** — Like/Dislike, "Did you wear it?", fit toggles, ratings, try-ons, saves, chat swaps |
| Eval visibility | **Silent logs** for now; `/admin/metrics` dashboard later if wanted |
| Target host | 187 (wardrobe VM, CPU) runs the engine; FashionCLIP/ALS are CPU-friendly |

### Still needed before Step A
- Per-individual **profile values** for each family member (§3.5) — or "skip profiles,
  rely on feedback learning" (slower cold start).
- Confirmation to **start Step A** (interaction log + family sharing + profile fields).
