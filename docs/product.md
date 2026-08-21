# Clueless Closet — Product Status & Roadmap

> Single source of truth for **current behavior** and **what's next** (2026-08-21).
> If this contradicts an older commit message or a loose "v0.x" note, this file wins.
> Full plan/API contract: `PLAN.md` · architecture/decisions: `docs/architecture.md`.

---

## 1. What it is

Clueless Closet (repo name `altacloset`) is a **self-hosted, Dockerized AI personal stylist**:

- keeps your **real wardrobe** (photos + name/category/color),
- **recommends outfits** from weather + activity + a free-form style prompt,
- **renders a chosen garment onto a photo of a person** (saved photo, upload, or webcam)
  using CatVTON + ComfyUI on a local GPU.

**Live right now:** `http://10.0.1.202:28085` (webapp on 202; ComfyUI `127.0.0.1:28190`).

---

## 2. Status (2026-08-21)

| Phase | Area | State |
|---|---|---|
| 1 | Accounts (email), weather, rule-based recommender, wardrobe | ✅ DONE |
| 2 | CatVTON try-on (ComfyUI, GPU) | ✅ DONE & working (~40s warm / 1m26s cold) |
| 4 | Wardrobe manager, saved outfits, image-quality, uniform + iPhone-first UI | ✅ DONE (this session) |
| 3 | LLM stylist (Ollama) | ⬜ not started |
| 5 | Migrate to target GPU box | ⬜ not started |

---

## 3. Feature tour (by tab)

### Outfit
- Live weather in **°F** for the user's location (default San Mateo, CA 94403; per-user override).
- Occasion dropdown + style prompt → **rule-based recommendation** with reasons & scores.
- Empty wardrobes return a friendly `empty_wardrobe` note.

### Try on
- **Person source**: saved photo / upload / webcam.
- **Look builder**: Top / Bottom / Dress selects (only garments **with photos**), plus
  *Use recommendation*, *Reset look*, *Save outfit*.
- **Image-quality feedback** inline for both person and garment (auto on change).
- **Progress panel**: Uploading → DensePose → SCHP → CatVTON → Finalizing, with an
  elapsed timer, so a ~40s GPU render doesn't look stuck.
- **Chained multi-garment try-on** (`/api/tryon/outfit`): top → bottom in sequence.

### Wardrobe
- **Add a garment**: name/category/color + upload **or** a product-page link
  (`Fetch details` auto-fills name/color/category and shows an image picker).
- Each card: image + category badge + name + a single **Edit** button.
- **Edit modal**: replace photo (Upload photo / Set from link — inline URL field),
  edit name / category / color, **Save**, **Delete**. Everything in one place.
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

---

## 5. UX / design decisions

- **iPhone-first** — most usage is on a phone; designs are tested on desktop and must
  hold at 390px. All future UI should follow the `@media (max-width:640px)` block.
- **Uniform grids** — fixed 3:4 image boxes make a mixed-aspect wardrobe look tidy.
- **One action per garment** — a single Edit button → modal keeps cards clean and
  mobile-friendly.
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
- **ComfyUI must be running** for try-on: `docker compose --profile gpu up -d comfyui`.
  Otherwise `/api/tryon*` → 503.
- **Embedded/preview browser**: `prompt()`/`confirm()` may be unsupported (use inline
  inputs); Playwright clicks can time out on "stability" (click via JS instead); the
  browser-tool filesystem is sandboxed (no file writes, no file-chooser uploads).
- `GET /api/wardrobe/{id}` does **not** exist (405) — verify via `GET /api/wardrobe`.
- **Webapp rebuild required** when `app/` changes (code is baked into the image):
  `docker compose up -d --build webapp`. `data/` is bind-mounted (survives rebuilds).
- **VS Code occupies ports** 28082/28188/28189 on 202 — use 28085 (webapp) / 28190
  (comfyui) on that host.

---

## 7. What's next (roadmap, prioritized)

### Near term — polish that makes it genuinely useful
1. **rembg background removal** for flat-lay garment images so CatVTON gets a clean
   cutout (currently background bleed lowers quality). `rembg` or a ComfyUI
   segment-anything node.
2. **Webcam → try-on polish**: downscale ≤1024px, retry button, iPhone camera-permission
   UX.
3. **Suitability on upload** — run image-quality when a base photo is uploaded and warn
   *before* saving; add a one-tap "delete low-quality base" in Account ("no point in
   using them").
4. **Whole-look try-on in one pass** — today we chain top→bottom sequentially; render a
   full look in a single CatVTON workflow for speed and fewer artifacts.
5. **parse-link browser fallback** — for Akamai/JS-heavy retailers (Abercrombie): when
   the server fetch fails, fall back to a headless/Playwright fetch inside the webapp so
   "Fetch details" works for any store.

### Phase 3 — LLM stylist
6. **Ollama (Qwen2.5 3B / Gemma 3 4B)** concurrent with CatVTON (~11.5GB total);
   `/api/chat` explains the pick in natural language.

### Phase 4 — product-ish
7. **Auto-tagging**: infer color/material/category from the garment image.
8. **Daily digest** ("wear this today" from weather + occasion) — Alta's daily outfit.
9. **Person profiles**: name multiple base photos (e.g. Sam) and try looks per person.
10. **OIDC/SSO** (Authelia/Keycloak) — swap boundary already exists
    (`auth.get_user_by_token`).

### Phase 5 — migration & hardening
11. Migrate to the target GPU box (`scripts/migrate-to-target.sh`) + smoke test.
12. **PWA / Add to Home Screen** for iPhone (standalone, icon, webcam permissions) —
    most usage is on a phone.
13. **Renderer upgrade path** when wanted: IDM-VTON (solo 16GB "quality mode") →
    FLUX.1-Kontext (GGUF ~12–16GB).

### Layout
14. Tablet breakpoint (~641–1024px) for the photo grid; verify landscape iPhone.

---

## 8. API surface (summary)

| Method & path | Purpose |
|---|---|
| POST `/api/auth/register` · `/login` · `/logout`, GET `/api/auth/me` | email + password accounts, bearer sessions |
| GET `/api/account`, POST `/api/account/password`, POST `/api/account/location` | account details / password / location |
| GET `/api/weather` | live weather (°F + °C), per-user location |
| GET `/api/wardrobe`, POST `/api/wardrobe` | list / add garment |
| POST `/api/wardrobe/parse-link` | extract name/color/category/images from a product page |
| POST `/api/wardrobe/{id}/image` · `/image-url` · GET `/api/wardrobe/{id}/image` | set (upload/link) / serve garment image |
| **PATCH `/api/wardrobe/{id}`** | **edit name/category/color** (this session) |
| DELETE `/api/wardrobe/{id}` | delete garment |
| GET/POST `/api/photos`, PATCH `/api/photos/{id}`, POST `/api/photos/{id}/default`, GET `/api/photos/{id}/image`, DELETE `/api/photos/{id}` | person photos (try-on bases) |
| POST `/api/image-quality` | score person/garment image (`kind=person\|garment`) |
| POST `/api/recommend` | rule-based outfit recommendation |
| POST `/api/tryon`, POST `/api/tryon/outfit` | single / chained try-on render |
| GET `/api/outfits`, POST `/api/outfits`, DELETE `/api/outfits/{id}` | saved looks (with render thumbnails) |
| GET `/api/uploads/{name}` | private try-on result image (owner-only) |

All data endpoints require `Authorization: Bearer <token>`. Full contract: `PLAN.md §6`.
