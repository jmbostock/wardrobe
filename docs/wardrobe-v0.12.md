# Clueless Closet — Wardrobe v0.12 (garment metadata, AI tag-read, dedup, import)

Session work from 2026-08-22. App version at `/health` is now **0.12.0** for the
metadata work below; the **orientation** addendum shipped later at v0.14.0 — see **§12**.

## 1. Garment metadata — auto-fill on BOTH add paths

Every garment can now carry **name, brand, color, category, sizes** (plus owned/rating).
Both ways of adding a garment pre-fill what they can; everything stays editable in the
garment detail card.

- **URL path** (`Fetch details` → `POST /api/wardrobe/parse-link`): now extracts
  **brand** (JSON-LD `brand` → `og:site_name`) and **sizes** (JSON-LD `size`/`offers`
  + on-page size pickers) on top of the existing name/color/category/images.
- **Upload path**: picking a photo calls **`POST /api/wardrobe/ai-fill`** — a small
  Ollama **vision model reads visible tags** (brand / color / category / sizes) and
  pre-fills the form. Never blocks: if the AI is unavailable it degrades gracefully.
  - Vision model: **`qwen2.5vl:3b`** (`OLLAMA_VISION_MODEL` env). Notes:
    - moondream was tried first and is **too weak** — it ignores the labeled-line
      prompt and hallucinates size lists, so it was rejected.
    - qwen2.5vl:3b follows the 5-line prompt (`NAME:/BRAND:/COLOR:/CATEGORY:/SIZES:`)
      and reads real tags (e.g. a pair of jeans → `Hollister`, `28,30`).
    - `app/aifill.py` parses the labeled-line output (JSON fallback for other models)
      and strips filler words ("Blank", "Not visible", "No visible brand name", …).

## 2. Brand & Color dropdowns (from the DB)

`GET /api/wardrobe/meta` returns `{brands, colors, schemas}`:
- **brands** = distinct brands found across the user's garments → any brand the AI /
  parse-link ever finds is automatically available.
- **colors** = the canonical color palette + colors already used in the wardrobe.
- Both the add form and the detail card use `<datalist>` type-ahead for **Brand** and
  **Color** (free text still allowed).

## 3. Type-aware sizes

Sizes now adapt to the garment type (`media.SIZE_SCHEMAS`, returned in `/api/wardrobe/meta`):

| Category | Size input | Stored as |
|---|---|---|
| Top / Dress / Outerwear | S/M/L/XL suggestions | `S,M,L` |
| **Bottom (pants)** | **Waist × Length** | `30W x 32L` |
| **Bra** (new category) | **Band × Cup** | `34C` |
| Footwear | numeric (5–11, half sizes) | `8.5` |
| Accessory | One size / OS | `One size` |

- Added **`bra`** to `WARDROBE_CATEGORIES` (also in parse-link category keywords and
  try-on `CLOTH_TYPE` → `upper`), and to the category dropdowns in the add form +
  detail card.
- Pants/bra use two fields; existing strings parse back correctly (`28,30` → waist 28 /
  length 30; `36DD` → band 36 / cup DD).

## 4. Wardrobe detail card (outfits-style)

- Tapping a wardrobe card opens a **detail card** (like the Outfits detail): full-size
  image, editable Name/Brand/Size/Category/Color, photo upload/set-from-link, owned
  checkbox, **rating**, and Delete. The old per-card **Edit** button was removed
  (`partials/edit_modal.html` deleted).
- **Rating is now a 0–10 step-1 range slider** (single line — the old 10-dot widget
  wrapped on narrow screens). Shared via `common.js` `bindRating()` with the Outfits
  detail card.

## 5. Near-duplicate detection (perceptual hash + color gate)

"Did I already scan this?" detection:

- **`app/phash.py`** — 64-bit **dHash** (Hamming distance) computed on the garment's
  **center crop** (not the whole frame — flat-lay backgrounds are otherwise dominant
  and make every similarly-shot garment look "similar"). Plus a coarse dominant-color
  class (also center crop) stored as `garments.color_sig`.
- Stored per garment: `garments.phash` and `garments.color_sig` (computed on image save).
- A garment is flagged as a near-duplicate only when **all three** match an existing
  garment: **same category**, **color compatible**, **center-crop dHash ≤ 8 bits**.
  - **Color gate:** if both garments carry a canonical color tag (e.g. the dropdown's
    red vs pink) those must match — otherwise fall back to the coarse photo color
    class. This killed the false positives: "olive joggers ≈ black swimsuit" AND
    "red one-piece ≈ pink polka-dot swimsuit" (both had photo-centers classifying as
    red, but their canonical colors red ≠ pink).
- Surfaced as: `⚠ similar to X` on the grid card, a note in the detail card, and a toast
  on add/upload.

## 6. HEIC (iPhone) support

- `pillow-heif` added; HEIC is detected (`imglink._is_heic`) and **converted to JPEG on
  save** (`media._heic_to_jpeg`), so everything downstream (serving, try-on) stays uniform.
- Needed because the imported photos were iPhone HEIC (and ffmpeg on 202 can't decode HEIC).

## 7. Bulk import script

**`scripts/bulk-import.py`** — folder → AI tag-read → create garment → upload image →
flag near-dups. Flags: `--include-dups`, `--skip-dups`, `--dry-run`.
For HEIC folders run it **inside the webapp container**:

```bash
docker cp ~/Downloads/<dir> altacloset-webapp:/tmp/photos
docker cp ~/altacloset/scripts/bulk-import.py altacloset-webapp:/tmp/bulk-import.py
docker exec -w /app -e PYTHONPATH=/app altacloset-webapp python /tmp/bulk-import.py \
  --dir /tmp/photos --email <you>@... --password ... --base http://localhost:8000
```

Gotchas learned: `pillow_heif` needs an explicit `register_heif_opener()`; `docker cp`
of a directory lands its *contents* at the destination path; Ollama vision can't read
HEIC (convert to JPEG first); upload/create responses must **re-fetch the garment after
saving the image** or `near_dup_of` is always null.

## 8. UI / CSS fixes

- `.field { min-width:0 }` — side-by-side fields no longer overflow/wrap into each other.
- Focus ring: `input:focus { outline-offset:2px }` so it stays clear of neighbors.
- Add form + detail card: Brand and Size are now stacked rows with room to breathe
  (previously cramped).

## 9. Data / account changes (IMPORTANT)

- **DB (additive migrations):** `garments.brand`, `garments.sizes`, `garments.phash`,
  `garments.color_sig`; new `bra` category value.
- **Account cleanup:** all 13 imported photos were moved to **`bostock@gmail.com`**
  (user id 3) — DB rows *and* image files (`data/wardrobe/3/`). The test account
  **`me@example.com` (id 2) was deleted** along with its test garments/outfits/photo
  and on-disk `data/wardrobe/2` + `data/uploads/2`.
- **Rule going forward:** the real user is `bostock@gmail.com`; do **not** create users
  or use demo/test accounts without permission. Remaining test accounts
  (`demo@example.com`, `verify@example.com`) are untouched.

## 10. Known follow-ups / review

- ~~**Jeans cluster #120–124** flagged as near-duplicates (dist 9–12)~~ — after the
  near-dup tightening (center-crop dHash ≤ 8 + canonical-color gate) these are no
  longer flagged; the earlier flags were background-dominated full-frame matches.
  If some of them genuinely are the same pair, merge/delete manually.
- #118 ("Black one-piece swimsuit") was re-categorized to **top** per your call.
- `scripts/tryon-test.sh` still references the deleted `me@example.com` account — needs a
  fix or removal.

## 11. Infra notes (host 202)

- Ports: webapp **28085**, comfyui **28190**, ollama **28114** (internal `ollama:11434`).
- Ollama service runs with `--profile gpu`; model pulled: `qwen2.5vl:3b`.
- Browser was left on the login screen — sign in as `bostock@gmail.com` to see the
  wardrobe (the agent does not have that account's password).

## 12. Garment orientation — deterministic, NEVER horizontal (2026-08-22, v0.14.0)

Addendum shipped after v0.12 — full write-up in **`docs/handoff-2026-08-22.md` §6**.
Short version (the user's hard rule: garment photos must **never** be horizontal):

- **Final approach (deterministic, no exceptions):** `media.normalize_orientation`
  applies EXIF righting, accepts **only** a portrait-preserving 180° flip (from the
  tag-reader `aifill.ai_orientation`, for an upside-down garment with a readable
  tag), and then **hard-guarantees** the result is portrait — 90/270 rotations are
  never applied and any landscape frame is rotated back to portrait.
- **Reverted experiment:** an edge-detection "look, then rotate"
  (`ai_upright_rotation` / `_top_edge`, commit `5a2371d`) asked the model which
  edge the garment's top is on, but the small VLM's answers were unreliable (it
  flip-flopped between left/down/up/right on the same folded flat-lay), and the 90°
  rotations turned **#119/#122/#124 landscape** — violating the user's rule. The
  code was removed and the backfill reverted those three to portrait.
- **Result:** all 20 stored photos are **portrait AND upright** (verified with the
  model against known-upright controls).
- **Files:** `app/media.py` (`normalize_orientation`, `save_garment_image`),
  `app/aifill.py` (`ai_orientation` tag-reader only), `scripts/backfill_orientation.py`.
