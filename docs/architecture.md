# altacloset — Architecture & Decision Log

## Services (all in Docker, single GPU box)

| Service | Role | Port | GPU | VRAM |
|---|---|---|---|---|
| `webapp` | FastAPI app (recommend, weather, tryon, chat) | 28085 on 202 (default 28082, 0.0.0.0 for LAN) | no | — |
| `comfyui` | Renderer host (CatVTON try-on) | 28190 on 202 (default 28188, internal :8188) | yes | ~8GB |
| `ollama` | LLM stylist (phase 3) | 28114 (internal) | yes | ~3–3.5GB |

> 202 is a busy box — VS Code occupies 28082/28188/28189, so the running stack uses 28085/28190.

## Decision log

| # | Decision | Date | Rationale / notes |
|---|---|---|---|
| 1 | CatVTON over IDM-VTON/OOTD/FLUX for MVP | 2026-08-21 | <8GB VRAM @1024×768, garment-image-only input, official ComfyUI workflow. IDM-VTON needs full 16GB (solo quality mode); FLUX needs GGUF quant (~12–16GB) → phase 4 upgrade path |
| 2 | Rule-based recommender before LLM | 2026-08-21 | Deterministic, zero GPU/latency, explainable. LLM (Qwen2.5 3B / Gemma 3 4B via Ollama) only explains picks in phase 3 |
| 3 | Open-Meteo (no key) + HA override | 2026-08-21 | Portable across machines, no credentials to move; HA override available since the homelab already runs Home Assistant (10.0.1.168) |
| 4 | ComfyUI official CUDA image + baked CatVTON node | 2026-08-21 | Custom-node install baked into image build → target machine needs zero manual steps (portability requirement) |
| 5 | All state in `data/` bind-mounts + `models` volume; all config in `.env` | 2026-08-21 | Guarantees `docker compose up` parity between 202 and the target machine |
| 6 | Services bound to 127.0.0.1, Caddy in front if LAN/TLS needed | 2026-08-21 | Keep ML endpoints off the LAN; match homelab convention (Caddy already used in `compose/caddy`) |
| 7 | Multi-user: local accounts (PBKDF2 + bearer sessions) + per-user wardrobe/uploads | 2026-08-21 | Self-contained & portable (no external IdP). All rows carry `user_id`; try-on results are private per user. OIDC/SSO is the phase-3 upgrade — `auth.get_user_by_token` is the swap boundary |
| 8 | Image-quality = pure-PIL heuristics (no ML) | 2026-08-21 | `POST /api/image-quality` scores person/garment; deterministic, no extra GPU/RAM. Reused by the Account **base-suitability chips** (frontend-only) so bad base photos are obvious before a ~40s GPU render |
| 9 | iPhone-first responsive UI | 2026-08-21 | Most usage is on iPhone. `@media (max-width:640px)` block; uniform `aspect-ratio:3/4` card grid (2-col mobile); card buttons stack full-width on mobile. Verified no overflow at 390px |
| 10 | One Edit modal per garment (consolidated actions) | 2026-08-21 | Cards show a single **Edit** button; photo upload/from-link, name/category/color, Save + Delete all live in one modal (per user request). Backed by new `PATCH /api/wardrobe/{id}` + `Wardrobe.update()` |
| 11 | Multi-page app (Jinja2) over a single-page tabbed UI | 2026-08-21 | `index.html` (1,119 lines) split into per-page Jinja templates extending `base.html`, shared `app.css` + `common.js`; `main.py` (679 lines) split into per-domain routers (`app/routes/`) with shared `deps.py`/`store.py`/`media.py`. Each page is a thin server shell; data stays a JSON API with Bearer-token auth (client guard → `/login` on 401) |
| 12 | Ratings out of 10 on garments + saved outfits | 2026-08-21 | `rating` column on `garments` + `outfits` (additive migration); shared 10-dot tap widget in `common.js`; `PATCH /api/outfits/{id}` + `rating` on garment PATCH |
| 13 | Try-on chat re-render + Saved-image mode | 2026-08-21 | After a render, Enter sends a note → `/api/tryon/outfit` with `base_result` re-renders from the last image. **Saved-image mode** picks a saved outfit render (no look needed); empty `garment_ids` + base = refine without re-adding garments (no duplicate files). CatVTON is garment-only, so the actual visual edit awaits a promptable model (Phase 5) |
| 14 | PWA for iPhone home-screen | 2026-08-21 | `manifest.webmanifest` + `sw.js` (app-shell cache, API network-only, secure-context only) + PIL-generated icons + `viewport-fit=cover`/`env(safe-area-inset-*)` so standalone mode clears the notch + home indicator |
| 15 | SVD motion clips (async, queued) | 2026-08-22 | `svd_xt.safetensors` symlinked into the comfyui image (recreate loses the symlink — weight persists). `POST /api/tryon/clip` submits → `GET /api/clips/{id}` polls; runs server-side so navigating away doesn't stop it. `clips` table tracks queued→running→done/error |
| 16 | Outfits auto-save every render | 2026-08-22 | `POST /api/tryon/outfit` creates a NEW saved outfit per render (dedupe removed — re-rendering previously looked like "nothing saved"). Stores `person_photo_id`/`person_url` so the source photo is known |
| 17 | Photo auto-pick driven by the GARMENT (vision) | 2026-08-22 | `GET /api/photos/best-for-garment/{id}` → `app/photopick.py` sends garment + every saved photo to Ollama `qwen2.5vl:3b` in ONE multi-image call, scored mostly on outfit type/coverage match (swimsuit garment → swimsuit-ish photo, dress → dress photo). Auto-selects the best in the Try-on dropdown; a manual pick wins. Falls back to `imageqa.suitability` (pure-PIL, category-nudged) when the model is down; vision-skipped photos filled with the heuristic |
| 18 | Garment metadata + AI tag-read + auto-orient | 2026-08-22 | `garments.brand`/`sizes` columns; `POST /api/wardrobe/ai-fill` reads visible tags with `qwen2.5vl:3b` (moondream rejected — ignores multi-line prompts); parse-link extracts brand/sizes from product pages. Uploads are EXIF-righted + portrait-normalized (deterministic) with an optional manual/rotational correction path |
| 19 | PWA cache discipline: bump `CACHE` per release | 2026-08-22 | `sw.js` serves static assets cache-first — without bumping `closet-v2 → v3`, phones kept serving the stale v0.12 `tryon.js` and never saw the auto-pick. **Bump `const CACHE` on every JS/CSS release.** |
| 20 | VRAM auto-unload (`OLLAMA_KEEP_ALIVE=2m`) & graceful VLM timeout (10s) + `fast` mode | 2026-08-23 | Ollama unloads Qwen2.5-VL after 2m idle so CatVTON has full ~14-15GB VRAM headroom. `photopick` timeout reduced 60s → 10s and supports `fast=true` pure-PIL suitability ranking (<2ms) |
| 21 | Bitwise integer Hamming distance for near-duplicate dHash (`phash.py`) | 2026-08-23 | Replaced hex char iteration with CPU-native `(val_a ^ val_b).bit_count()` for 64-bit dHash comparison |

## Frontend code layout (2026-08-21)

```
app/
  main.py            thin entrypoint (router includes + /health)
  deps.py            get_current_user (auth boundary)
  store.py           wardrobe/outfits singletons
  media.py           garment-image + product-fetch + orientation helpers
  imageqa.py         pure-PIL person/garment QA + suitability() fallback scorer
  photopick.py       best saved photo FOR a garment (vision outfit-match + fallback)
  aifill.py          optional AI tag-read (brand/color/category/sizes) via Ollama vision
  svd.py / clips.py  SVD motion-clip client + async job store
  routes/            pages, auth, account, photos, wardrobe, outfits,
                     tryon, recommend, image (one router per domain)
  templates/         base.html + auth/suggest/tryon/wardrobe/outfits/account
                     (garment edit lives in the wardrobe detail card, not a partial)
  static/            css/app.css · js/{common,auth,suggest,tryon,wardrobe,
                     outfits,account}.js · manifest.webmanifest · sw.js (cache v3) ·
                     icons/ (PIL-generated)
```

## Performance targets (5060 Ti 16GB)

- CatVTON: ~10–20s/image
- rembg: ~1–3s
- 3–4B LLM: ~50–80 tok/s
- Recommendation: <10ms (rule-based)
