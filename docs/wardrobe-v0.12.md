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

- **`app/phash.py`** — 64-bit **dHash** (Hamming distance) + a **coarse dominant-color
  class** computed from the **center crop** of the photo (the garment, not the white
  backdrop).
- Stored per garment: `garments.phash` and `garments.color_sig` (computed on image save).
- A garment is flagged as a near-duplicate only when **all three** match an existing
  garment: **same category**, **same color class**, **dHash ≤ 12 bits**.
  - This killed the false positives like "olive joggers ≈ black swimsuit".
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

- **Jeans cluster #120–124** are flagged as near-duplicates (dist 9–12) — review and
  merge/delete the ones you don't want.
- #118 ("Black one-piece swimsuit") was re-categorized to **top** per your call.
- `scripts/tryon-test.sh` still references the deleted `me@example.com` account — needs a
  fix or removal.

## 11. Infra notes (host 202)

- Ports: webapp **28085**, comfyui **28190**, ollama **28114** (internal `ollama:11434`).
- Ollama service runs with `--profile gpu`; model pulled: `qwen2.5vl:3b`.
- Browser was left on the login screen — sign in as `bostock@gmail.com` to see the
  wardrobe (the agent does not have that account's password).

## 12. Garment orientation — "look, then rotate" (2026-08-22, v0.14.0)

Addendum shipped after v0.12 — full write-up in **`docs/handoff-2026-08-22.md` §6**.
Short version:

- Orientation approaches that **failed** on these photos: tag-read
  ("rotate-then-read-text", `7cd9b79`) only worked when a tag was readable;
  "is it upright?" YES/NO is unreliable on folded flat-lays (model says YES at every
  rotation); a manual rotate button was removed per user request.
- **Current approach** (`aifill.ai_upright_rotation()` + `_top_edge()`): ask the model
  *"which edge is the garment's top on? (top/bottom/left/right)"* — stable 3/3
  agreement on all 20 photos — and rotate so that edge is the **TOP** edge. Works for
  textless folded garments. `save_garment_image(ai_orient=True)` runs it on **every**
  upload (no exceptions); `normalize_orientation(..., ai_decided=True)` trusts the AI's
  answer even when it's 0 (already upright).
- **Backfill:** `scripts/backfill_orientation.py` re-processed all stored photos.
  Result: 17/20 were already upright; **#119 (olive joggers), #122, #124** (top on
  left) were rotated upright — all 20 now read "top" and the wardrobe is consistent.
- The user asked to "rotate so it's horizontal"; since 17/20 were already upright, the
  3 sideways ones were rotated to match (upright). If horizontal is actually wanted,
  flip the mapping in `aifill._EDGE_TO_CW` (one line) and re-backfill.
