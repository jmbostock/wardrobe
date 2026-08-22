# Clueless Closet

Self-hosted, Dockerized personal stylist — keeps your real wardrobe, recommends
outfits from **weather + activity + prompt**, and renders the recommended clothes
onto a **photo of a person** (stored image, upload, or live webcam), à la Alta Daily.

- Test host: `10.0.1.202` (RTX 5060 Ti 16GB) — then migrates to a second box with the same GPU.
- Everything runs in Docker; all config via `.env`.
- **Live:** `http://10.0.1.202:28085` (webapp) · ComfyUI try-on on `127.0.0.1:28190`.

## Current status (2026-08-22)

- ✅ Phase 1 (email accounts, weather, rule-based recommender, wardrobe)
- ✅ Phase 2 (CatVTON try-on via ComfyUI, GPU) — working end-to-end
- ✅ Phase 4 polish: wardrobe **detail/edit card**, add-form **Clear**, saved
  outfits, **image-quality / base-suitability chips**, **SVD motion clips**
  (async 3s animated webp), **auto-saved outfits** (every render → new card),
  uniform spacing + **iPhone-first** UI
- ✅ **Auto-pick the best saved photo per garment** (v0.13.0): the photo that
  generates a try-on is chosen by outfit match with the garment being tried on
  (Ollama vision, `qwen2.5vl:3b`), with a pure-PIL fallback
- ✅ Garment **AI tag-read** (brand/color/category/sizes from an uploaded photo)
  + **auto-orient** (EXIF-righted, portrait-normalized) on upload
- ⬜ Phase 3 (LLM stylist) and Phase 5 (migration) — not started

**What's next:** `docs/product.md §7` (prioritized roadmap). **Full plan:** `PLAN.md`.
**Fresh-session handoff (tests, deploy, open questions):** `docs/handoff-2026-08-22.md`.
**Garment-metadata work:** `docs/wardrobe-v0.12.md`.

## Quickstart (webapp + recommender)

```bash
cp .env.example .env
docker compose up -d webapp
curl http://127.0.0.1:28082/health          # port 28085 on 202

# create an account (email-based), then use the returned token for everything else
TOKEN=$(curl -s -X POST http://127.0.0.1:28082/api/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"email": "you@example.com", "password": "a-secret-pass"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')

curl -s -X POST http://127.0.0.1:28082/api/recommend \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"activity": "office", "prompt": "professional but comfortable"}'
```

Open `http://127.0.0.1:28082/` in a browser — register/login there, then the
UI handles tokens automatically. Each account has its own wardrobe and results.

## Stack

| Piece | Tech |
|---|---|
| Webapp | FastAPI (`services/webapp`) |
| Auth | Local accounts (PBKDF2 + bearer sessions), per-user isolation |
| Recommender | Rule-based scoring engine (CPU) → LLM garnish later |
| Try-on | ComfyUI + CatVTON (GPU, <8GB VRAM) |
| LLM (phase 3) | Ollama + Qwen2.5 3B / Gemma 3 4B; `qwen2.5vl:3b` vision for AI tag-read + photo auto-pick |
| Weather | Open-Meteo (no key) with Home Assistant override |

## Docs

- `docs/product.md` — **current-state write-up + prioritized roadmap** (what's built, what's next)
- `PLAN.md` — full project plan, phases, API contract, migration
- `docs/architecture.md`, `docs/recommender.md`, `docs/tryon-pipeline.md`, `docs/host-202-notes.md`

## Migration to the target machine

```bash
# on 202
scripts/migrate-to-target.sh  # rsync data/ (models) + compose to target
# on target
docker compose up -d
```

See `PLAN.md §7` for the target-machine requirements checklist.
