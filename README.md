# Clueless Closet

Self-hosted, Dockerized personal stylist — recommends outfits from your own
wardrobe (weather + activity + prompt) and renders them onto a photo of a person
(stored image or live webcam), à la Alta Daily.
Recommends outfits from **weather + activity + prompt**, then renders the recommended
clothes onto a **photo of a person** (stored image or live webcam capture).

- Test host: `10.0.1.202` (RTX 5060 Ti 16GB) — then migrates to a second box with the same GPU.
- Everything runs in Docker; all config via `.env`.

## Quickstart (Phase 0/1 — CPU-only webapp + recommender)

```bash
cp .env.example .env
docker compose up -d webapp
curl http://127.0.0.1:28082/health

# create an account, then use the returned token for everything else
TOKEN=$(curl -s -X POST http://127.0.0.1:28082/api/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"username": "you", "password": "a-secret-pass"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')

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
| LLM (phase 3) | Ollama + Qwen2.5 3B / Gemma 3 4B |
| Weather | Open-Meteo (no key) with Home Assistant override |

## Docs

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
