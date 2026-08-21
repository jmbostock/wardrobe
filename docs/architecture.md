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

## Performance targets (5060 Ti 16GB)

- CatVTON: ~10–20s/image
- rembg: ~1–3s
- 3–4B LLM: ~50–80 tok/s
- Recommendation: <10ms (rule-based)
