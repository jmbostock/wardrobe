# Clueless Closet — Product Status & Roadmap

> Single source of truth for **current behavior** and **what's next** (2026-08-22).
> If this contradicts an older commit message or a loose "v0.x" note, this file wins.
> Full plan/API contract: `PLAN.md` · architecture/decisions: `docs/architecture.md`.

---

## 1. What it is

Clueless Closet (repo name `cluelesscloset`) is a **self-hosted, Dockerized AI personal stylist**:

- keeps your **real wardrobe** (photos + name/category/color),
- **recommends outfits** from weather + activity + a free-form style prompt,
- **renders a chosen garment onto a photo of a person** (saved photo, upload, or webcam)
  using **Qwen-Image-2.1** + ComfyUI on a local GPU.

**Live right now:** `http://10.0.1.187:28085` (webapp on 187; Qwen renderer on `10.0.1.202:8188`).

---

## 2. Status (2026-09-23)

| Phase | Area | State |
|---|---|---|
| 1 | Accounts (email), weather, rule-based recommender, wardrobe | ✅ DONE |
| 2 | Try-on — **Qwen-Image-2.1 image-edit** (ComfyUI on 202, GPU) | ✅ DONE & working (~60–120s per pass) |
| 2b | **Refine this outfit** — promptable edit on a saved render | ✅ DONE (2026-09-23) |
| 2c | CatVTON / IDM-VTON stack | ❌ REMOVED (2026-09-23, ~33 GB reclaimed) |
| 4 | Wardrobe manager, saved outfits, image-quality, uniform + iPhone-first UI | ✅ DONE |
| 4b | Wardrobe metadata (brand/color/sizes, AI tag-read, dedup) + orientation | ✅ DONE (2026-08-22) |
| 4c | **De-backgrounded wardrobe cards** — cutouts shown on the app panel | ✅ DONE (2026-09-23, v0.45.0) |
| 3 | LLM stylist (DeepSeek API) | ⬜ not started |
| 5 | Migrate to target GPU box | ⬜ not started |

---

## 3. Feature tour (by tab)

### Outfit (Suggest)
- Live weather in **°F** for the user's location (default San Mateo, CA 94403; per-user override).
- **Consolidated stylist chat** (v0.28.0): the occasion compose bar (activity /
  style hint / owned-only / **Suggest outfit**) sits at the top of the Cher chat
  card. Hitting **Suggest outfit** posts the pick straight into the thread as a
  rich Cher message — garment photos per slot, a **Why this outfit** reasoning
  list, and a **Try it on →** button — and you keep chatting with her from there.
- Empty wardrobes return a friendly note from Cher in the thread.

### Try on
- **Person source**: saved photo / upload / webcam.
- **Look builder**: tap **Top / Bottom / Dress / Swimsuit** to open a **photo picker**
  of every garment in that category (only garments **with photos**; shows
  name/brand/size). The picked garment's photo is shown in the row, so it's always
  clear what's being tried on. Tap the per-slot **✕** (or *None* in the picker) to
  clear just that one, or *Reset look* to clear all. Also *Use recommendation*.
- **Image-quality feedback** inline for both person and garment (auto on change).
- **Progress panel**: Uploading → reading photo+garments (Qwen3-VL) → rendering
  outfit (Qwen-Image-2.1) → Finalizing, with an elapsed timer, so a ~60–120s GPU
  render doesn't look stuck.
- **Chained multi-garment try-on** (`/api/tryon/outfit`): outfits of 3+ garments
  are rendered in several passes (lowers first, then uppers — never mixed), because
  a single pass with 3 references collapses.

### Wardrobe
- **Add a garment**: name/brand/color/category/sizes + upload **or** a product-page
  link (`Fetch details` → `POST /api/wardrobe/parse-link` auto-fills name/brand/
  color/category/sizes and shows an image picker).
- **AI tag-read on upload**: picking a photo calls `POST /api/wardrobe/ai-fill`
  (Ollama `qwen2.5vl:3b`) which reads brand/color/category/sizes off visible tags
  and pre-fills the form. Never blocks — if the AI is down it degrades gracefully.
- **Brand & Color dropdowns**: `GET /api/wardrobe/meta` returns the brands already
  in the wardrobe + the canonical color palette (with swatches); type-ahead
  `<datalist>` in both the add form and the detail card. Any brand/color the AI or
  parse-link finds is stored in the DB and becomes available automatically.
- **Type-aware sizes** (`media.SIZE_SCHEMAS`): top/dress/outerwear = S,M,L;
  **bottom = Waist×Length** (`30W x 32L`); **bra = Band×Cup** (`34C`);
  footwear = numeric; accessory = One size.
- Each card: image + category badge + name + a **⚠ similar to X** flag when the
  photo is a near-duplicate.
- **De-backgrounded cards** (2026-09-23): where a garment has been through the
  background-removal pass it is shown as a **cutout on the app's own slate
  panel** (the transparent PNG the app serves), not as a photo of a garment on
  a duvet. The original photo is still what every edit acts on, and the cutout
  is display-only — the renderer still gets the plain-white `<gid>.clean.png`.
  Garments without a cutout fall back to the original photo, so nothing ever
  disappears.
- **Detail card** (tap the card): full-size image, editable name/brand/size/
  category/color (with swatch), photo upload / set-from-link, owned checkbox,
  **rating 0–10 step-1 slider**, near-dup note, used-in-N-outfits, and Delete.
  The old per-card **Edit** button was removed.
- **Near-duplicate detection**: 64-bit dHash of the garment **center crop** (not
  the background) + same category gate + a **canonical-color gate** (dHash ≤ 8 bits,
  and if both garments carry a canonical color tag they must match) → `⚠ similar to
  X`. Kills the false positives: olive joggers ≈ black swimsuit, red one-piece ≈
  pink polka-dot swimsuit.
- **Orientation — never horizontal (automatic, no exceptions)**: every upload is
  EXIF-righted, only a portrait-preserving 180° flip (from the tag-reader) is ever
  applied, and the saved photo is **hard-guaranteed portrait** — a landscape frame
  is never produced. A **↻ Rotate 90°** button in the detail card is the manual
  escape hatch for a sideways item the reader got wrong; it re-saves through
  `save_garment_image`, so phash/colour stay consistent (and any derived
  `.clean.png` / `.cutout.png` is dropped, because it no longer matches the
  photo).
- **Clear** button on the add form resets it when a link fetch fails or picks the
  wrong thing.

### Account
- Email, change password, location.
- **My photos** — person photos used for try-on, each with a **suitability chip**
  (`base <grade> <score>/100`, color-coded). Red = low-quality base → **skip it for
  try-on**. The default photo is pre-selected.
- **Saved outfits** — saved looks with rendered thumbnails; load or delete.

---

## 4. This session's work (2026-08-21)

### Abercrombie dress added + tried on
- `www.abercrombie.com` blocks server-side fetch (Akamai 403) → scraped the page in a
  real browser, used the open image CDN
  (`img.abercrombie.com/is/image/anf/<SKU>_prod1?policy=product-large` = 800×1000 flat
  product shot), added it via `POST /api/wardrobe`, then rendered it on the saved base
  photo with `/api/tryon/outfit`. Garment image scores **100/100 (Great)**.

### Base-image suitability (Account → My photos)
- Each saved person photo now shows a color-coded **suitability chip**, reusing
  `POST /api/image-quality` (`kind=person`, `photo_id`). Frontend-only — no schema
  change. Low scores (<55) get a red card border + "⚠ low-quality base" note.

### Uniform spacing + iPhone-first
- `.photos` grid: `repeat(auto-fill, minmax(170px,1fr))` + `gap:14px` on desktop →
  `repeat(2,1fr)` + `gap:10px` at `@media (max-width:640px)`.
- Cards use `aspect-ratio:3/4` image boxes (`object-fit:contain`) so **every card is
  the same shape** regardless of source aspect ratio.
- Card buttons equal-width (`flex:1`) on desktop; **stacked full-width** on mobile for
  touch targets.
- Mobile: tighter main/header padding, header tagline hidden, form fields full-width,
  saved-outfit rows wrap. Verified **zero horizontal overflow at 390px** (iPhone).
- Wardrobe, Account-photos, and the link-preview picker all share these classes.

### Wardrobe edit modal + Clear
- Cards reduced to a single **Edit** button; photo upload / from-link / name / category /
  color / Save / Delete all live in the **edit modal** (per user request).
- New backend: `PATCH /api/wardrobe/{id}` (`WardrobeUpdate`) + `Wardrobe.update()`
  (validated column set). Covered by `tests/test_wardrobe.py::test_update`.

### Wardrobe metadata, dedup, dropdowns, sizes & orientation (2026-08-22)
Full write-up: **`docs/wardrobe-v0.12.md`** (metadata / dedup / dropdowns / sizes) and
**`docs/handoff-2026-08-22.md` §6** (orientation). Highlights:
- **Metadata auto-fill on BOTH add paths** — the URL path (`parse-link`) extracts
  brand + sizes from JSON-LD/on-page pickers; the upload path uses an Ollama vision
  model (`qwen2.5vl:3b`) to read tags. moondream was tried and is too weak (ignores
  the prompt, hallucinates size lists) — rejected.
- **Brand & Color dropdowns backed by the DB** — `GET /api/wardrobe/meta`;
  `normalize_color()` maps free-text variants onto a canonical palette (navy blue→
  navy, grey→gray, olive green→olive…).
- **Type-aware sizes** — pants waist×length, bra band×cup, per-category schemas
  (see Wardrobe feature tour above).
- **Near-duplicate detection** — center-crop dHash (≤ 8 bits) + same category
  + canonical-color gate (red ≠ pink) so only genuinely identical re-scans flag
- **HEIC support** — pillow-heif converts iPhone photos to JPEG on save.
- **Account fix** — all imported photos moved to `bostock@gmail.com` (user 3); the
  test account `me@example.com` (user 2) was **deleted**. Rule: do **not** create
  users or use demo/test accounts without permission.
- **Orientation — deterministic, never horizontal** (v0.14.0): EXIF righting +
  an optional 180° flip from the tag-reader + a hard guarantee the saved photo is
  portrait. An earlier "look, then rotate" edge-detection pass was reverted — the
  small VLM's up/down/sideways answers were unreliable and its 90° rotations made
  some photos landscape (forbidden). All 20 photos are portrait + upright.
- **Try-on look photo pickers** (v0.15.0): tapping **Top / Bottom / Dress** in the
  look builder opens a modal grid of **every garment photo in that category**
  (name/brand/size shown; the current pick is outlined). The chosen garment's
  photo is shown in the look row, so it's clear what's being tried on — no more
  guessing between several similar names (e.g. several jeans). The old text
  dropdowns stay as hidden `<select>`s so all existing logic (recommendation
  fill, auto best-photo pick) is unchanged.
- **Clear one slot + rotate 180 + swimsuit category** (v0.16.0): (1) each look row
  has a per-slot **✕** and the picker has **None — clear this slot**, so you can
  clear one category without resetting the whole look. (2) The garment detail card
  has **↻ Rotate 180°** (`POST /api/wardrobe/{id}/rotate`) — only 180° is offered
  because photos are guaranteed never-horizontal (90/270 would turn a portrait
  sideways); it re-saves through `save_garment_image` so near-dup stays correct.
  (3) New **swimsuit** category (WARDROBE_CATEGORIES, size schema, AI/parse-link
  keywords `bikini`/`one-piece`/`swim trunks`/`boardshorts`, one-piece → swimsuit),
  recommended as a hot-weather one-piece for **beach** (new `beach` occasion tag;
  warmth 1 defaults), never paired with a bottom, and never suggested for the
  office. Try-on look builder gained a **Swimsuit** row.
- **Full-page sheets + swipe to close + big ✕** (v0.17.0): every overlay — the
  try-on photo picker, the garment detail card, the outfit detail card, and the
  lightbox — is now a **full-page sheet** (not a small hover card): it covers the
  whole screen, the page underneath is scroll-locked while it's open (swiping no
  longer drags the background), a **swipe down** dismisses it, and the ✕ close
  button is a big 48px touch target. On phones the look picker shows **one
  full-width garment image per row** so the whole photo is visible when choosing.
- **Look builder = Top / Bottom / Full** (v0.18.0): the Dress and Swimsuit rows
  are merged into a single **Full** row — one-piece items (dresses, swimsuits,
  jumpsuits) that don't need a separate top/bottom. Picking a Full garment
  clears top/bottom. Sheet CSS hardened for older iOS (explicit offsets, flex
  stretch instead of `dvh`).
- **Sticky sheet header + prominent None** (v0.19.0): every sheet has a sticky
  top bar (grab, title, big ✕ close — plus the **None — clear this slot** button
  in the look picker) that stays frozen while the garment list scrolls, so you
  can always close or clear without scrolling back up. The None button is now a
  real bordered button with a red ✕, clearly separated from the first photo.
- **Flush-at-top sticky bar** (v0.20.0): the sheet panel lost its padding (moved
  into a `.body` wrapper) so the sticky bar (grab + title + ✕ + None) sits
  exactly at the top edge (y=0) and garments slide up right behind it. The grab
  handle is a thin 4px line.
- **Rotate 180° shows immediately** (v0.21.0): the rotate endpoint already
  rewrote the file, but the browser served the same-URL image from cache, so the
  detail card looked unchanged. Garment + person photo image endpoints now send
  `Cache-Control: no-store`, and the frontend cache-busts every garment image
  fetch (`?v=timestamp`), so clicking ↻ Rotate 180° updates the photo right away.
- **Rotate is cache-proof on the phone** (v0.22.0): `authImageUrl` now fetches
  with `cache:'no-store'` too, so every image shown (detail card, grid, picker,
  look rows) always loads the latest bytes. The rotate button also dims the
  current photo instantly and re-shows it rotated (clear feedback even if the
  180° flip is subtle).
- **Detail card tidy-up** (v0.23.0): removed the "used in N saved outfits" line
  from the garment detail card and added a gap between the photo and the Name
  field. (`used_count` is still kept for the wardrobe "Most used" sort.)
- **Recommendations show photos** (v0.24.0): `/api/recommend` now returns
  `has_image` on each garment, and the Suggest page renders the top/bottom/
  outerwear/footwear/accessory photos as thumbnails (color swatch fallback when
  a garment has no photo).
- **Bigger recommendation thumbnails** (v0.25.0): the photo on each suggested
  garment card is now 88×112 (2× the original), with a border, so the top/
  bottom/etc are clearly visible.
- **Try-on page decluttered** (v0.27.0): removed the long "Pick a top, a bottom…"
  hint/instruction text from the Try-on look builder; the page now just has the
  look rows, Owned-only, and a **full-width horizontal Try on** button.
  (The v0.26.0 Suggest-page declutter was reverted — it was a misdirected edit.)
- **Suggest + Chat consolidated** (v0.28.0): the segmented left-panel
  result/reasoning cards are gone — the Suggest page is now a single stylist
  surface. The occasion compose bar lives at the top of the Cher chat card, and
  **Suggest outfit** posts the pick INTO the thread as a rich Cher message
  (garment photos per slot, "Why this outfit" reasoning, and a Try it on →
  button on the card). You keep talking to Cher from there, and the conversation
  survives a page refresh (history is restored from the session). New
  `POST /api/suggest` does the recommend + seeds the chat; `/api/recommend` is
  unchanged for the Try-on page's "Use recommendation".
- **Recommendation reads like a real chat message** (v0.29.0): Cher's pick is
  now ONE bubble — the intro prose carries the "why" woven in naturally
  (weather/warmth, dress code, color harmony, layers, style hint) alongside the
  garment photos. The separate "Why this outfit" card was removed — it just
  repeated the prose. Items live inside the chat, so you can keep talking to
  Cher about them.

---

## 5. UX / design decisions

- **iPhone-first** — most usage is on a phone; designs are tested on desktop and must
  hold at 390px. All future UI should follow the `@media (max-width:640px)` block.
- **Uniform grids** — fixed 3:4 image boxes make a mixed-aspect wardrobe look tidy.
- **Tap-through detail card** — one tap on a wardrobe/outfit card opens a detail
  card (image, metadata, rating, delete); no per-card Edit button cluttering the grid.
- **Suitability chips** — surface bad base photos *before* a ~40s GPU render is wasted.
- **Deterministic over clever** — recommender is rule-based and explainable; image
  quality is pure-PIL heuristics; LLM is only a Phase-3 garnish.

---

## 6. Known gotchas & workarounds

- **Abercrombie / Akamai**: the HTML page 403s server-side `httpx`/curl, so
  `/api/wardrobe/parse-link` fails on those stores. Workaround: open the page in a real
  browser, scrape `<img>`/og:image/JSON-LD, and POST the CDN image URL directly
  (`img.abercrombie.com …policy=product-large`). A proper in-app browser fallback is on
  the roadmap (§7.5).
- **The Qwen renderer must be running** for try-on/refine: it is a separate
  ComfyUI on 202 (`QWEN_COMFYUI_URL`), not a service in this repo's compose file.
  Otherwise `/api/tryon*` and `/api/outfits/{id}/refine` → 503.
- **Embedded/preview browser**: `prompt()`/`confirm()` may be unsupported (use inline
  inputs); Playwright clicks can time out on "stability" (click via JS instead); the
  browser-tool filesystem is sandboxed (no file writes, no file-chooser uploads).
- `GET /api/wardrobe/{id}` does **not** exist (405) — verify via `GET /api/wardrobe`.
- **Webapp rebuild required** when `app/` changes (code is baked into the image):
  `docker compose up -d --build webapp`. `data/` is bind-mounted (survives rebuilds).
- **VS Code occupies ports** 28082/28188/28189 on 202 — the webapp uses 28085
  (on 187) and the Qwen renderer uses 8188 (on 202).

---

## 7. What's next (roadmap, prioritized)

> What **you** (the user) can do to make the app better — feedback, ratings,
> profile + garment data, base photos, de-backgrounding the real wardrobes — is
> in **`docs/next-steps-2026-09-23.md`**. This section is what's next in the
> *code*.

### Near term — polish that makes it genuinely useful
1. ✅ **DONE** — garment background removal (`<gid>.clean.png`, garment on plain
   white) so the renderer sees the garment, not the flat-lay backdrop.
2. **Webcam → try-on polish**: downscale ≤1024px, retry button, iPhone camera-permission
   UX.
3. **Suitability on upload** — run image-quality when a base photo is uploaded and warn
   *before* saving; add a one-tap "delete low-quality base" in Account ("no point in
   using them").
4. ✅ **DONE** — whole-look try-on in one pass. Qwen takes every garment as a
   reference, so there is no sequential chain for ≤2 garments and a short
   lowers-then-uppers schedule for 3+.
5. **parse-link browser fallback** — for Akamai/JS-heavy retailers (Abercrombie): when
   the server fetch fails, fall back to a headless/Playwright fetch inside the webapp so
   "Fetch details" works for any store.
6. **More refine presets** — the Refine box takes free text today; a few one-tap
   chips ("warmer light", "full-body shot", "walking pose") would make it
   discoverable.

### Phase 3 — LLM stylist
7. **DeepSeek API** for `/api/chat` (zero VRAM — see `docs/architecture.md` #22);
   the pick is already explained by the deterministic digest.

### Phase 4 — product-ish
8. **Auto-tagging**: infer color/material/category from the garment image.
9. **Daily digest** ("wear this today" from weather + occasion) — Alta's daily outfit.
10. **Person profiles**: name multiple base photos (e.g. Sam) and try looks per person.
11. **OIDC/SSO** (Authelia/Keycloak) — swap boundary already exists
    (`auth.get_user_by_token`).

### Phase 5 — migration & hardening
12. Migrate to the target GPU box (`scripts/migrate-to-target.sh`) + smoke test.
13. **PWA / Add to Home Screen** for iPhone (standalone, icon, webcam permissions) —
    most usage is on a phone.
14. **Decide on the vestigial engines** — SVD motion clips (~9 GB of weights on
    202) and InstructPix2Pix (~7 GB) are both still installed and code-referenced
    but no longer reachable from the UI. Delete, or re-expose.

### Layout
14. Tablet breakpoint (~641–1024px) for the photo grid; verify landscape iPhone.

---

## 8. API surface (summary)

| Method & path | Purpose |
|---|---|
| POST `/api/auth/register` · `/login` · `/logout`, GET `/api/auth/me` | email + password accounts, bearer sessions |
| GET `/api/account`, POST `/api/account/password`, POST `/api/account/location` | account details / password / location |
| GET `/api/weather` | live weather (°F + °C), per-user location |
| GET `/api/wardrobe`, POST `/api/wardrobe` | list / add garment (name/brand/color/category/sizes) |
| POST `/api/wardrobe/parse-link` | extract name/brand/color/category/sizes/images from a product page |
| POST `/api/wardrobe/ai-fill` | Ollama vision tag-read of an uploaded garment photo (brand/color/category/sizes) |
| GET `/api/wardrobe/meta` | `{brands, colors, color_hex, schemas}` for the dropdowns / size inputs |
| POST `/api/wardrobe/{id}/image` · `/image-url` · GET `/api/wardrobe/{id}/image` | set (upload/link) / serve garment image (upload auto-orients) |
| **PATCH `/api/wardrobe/{id}`** | **edit name/brand/color/category/sizes/rating** |
| DELETE `/api/wardrobe/{id}` | delete garment |
| GET/POST `/api/photos`, PATCH `/api/photos/{id}`, POST `/api/photos/{id}/default`, GET `/api/photos/{id}/image`, DELETE `/api/photos/{id}` | person photos (try-on bases) |
| GET `/api/photos/best-for-garment/{garment_id}` | pick the best saved photo for a garment by outfit match (Ollama vision; heuristic fallback) |
| POST `/api/image-quality` | score person/garment image (`kind=person\|garment`) |
| POST `/api/recommend` | rule-based outfit recommendation |
| POST `/api/suggest` | recommend + post the pick into the stylist chat thread (returns `session_id`) |
| POST `/api/tryon`, POST `/api/tryon/outfit` | single / chained try-on render |
| GET `/api/outfits`, POST `/api/outfits`, DELETE `/api/outfits/{id}` | saved looks (with render thumbnails) |
| GET `/api/uploads/{name}` | private try-on result image (owner-only) |

All data endpoints require `Authorization: Bearer <token>`. Full contract: `PLAN.md §6`.
