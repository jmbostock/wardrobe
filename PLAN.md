# altacloset — Local "Alta Daily" Clone — Project Plan

> Self-hosted, Dockerized, AI personal stylist. Recommend outfits from weather/activity,
> then render the recommended clothes onto a photo (stored or live webcam).
> Test on **202** (RTX 5060 Ti 16GB), then migrate to a second machine with the **same GPU**
> — everything must be portable (Docker-only, env-driven, no host hardcoding).

- Status: **PLANNING** (Phase 0 not started)
- Test host: `10.0.1.202` (pop-os)
- Target host: TBD — "another computer with the same GPU" (RTX 5060 Ti 16GB)

---

## 1. Goal & MVP Scope

### Full product (Alta Daily parity, eventual)
- Digital closet (snap photo → catalogued wardrobe with auto-tags)
- Virtual dressing room (try any outfit on a virtual avatar of you)
- Daily outfit recommendations (weather + closet + occasion/activity)
- Trip packing lists, wishlist, community looks

### MVP (this plan, first milestone)
1. **Multiple users**, each with their own account, wardrobe, photos, and try-on results
   (local auth: register/login, bearer sessions — OIDC is the phase-3 upgrade).
2. **Recommend** an outfit for the signed-in user from: free-form prompt + current weather + activity.
3. **Render** the recommended clothes onto a person photo — either a stored one or a
   webcam capture taken right now.
4. Hosted as a **webapp** (browser UI), all services in **Docker** on a single GPU box.

Phase 2+: LLM stylist chat, full-outfit try-on, wardrobe CRUD, trip packing.

---

## 2. Architecture

```
Browser (React/HTMX)
   │  getUserMedia (webcam) for live photo
   ▼
Caddy/nginx (reverse proxy, TLS)  ── 127.0.0.1
   ▼
altacloset-webapp  (FastAPI :8000)
   ├── /api/recommend   → rule-based recommender (CPU, <10ms)
   ├── /api/weather     → Open-Meteo (no key) or Home Assistant override
   ├── /api/tryon       → calls ComfyUI with CatVTON workflow → returns rendered photo
   └── (phase 2) /api/chat → Ollama (Qwen2.5 3B / Gemma 3 4B) explains the pick
   ▼                        ▼
ComfyUI (:8188, GPU)   Ollama (:11434, GPU)
   └── CatVTON node       └── small LLM (~3-3.5GB)
       (try-on diffusion, <8GB VRAM)
```

### VRAM budget on a 5060 Ti (16311 MiB)

| Service | VRAM | Notes |
|---|---|---|
| ComfyUI + CatVTON | ~8GB | SD1.5-based, 1024×768 bf16 — official figure |
| Ollama (3–4B, Q4) | ~3–3.5GB | Qwen2.5 3B / Gemma 3 4B |
| Webapp + segmentation | ~0.5GB | rembg runs on CPU/GPU-light |
| **Total (concurrent)** | **~11.5GB** | comfortable headroom |

> Rule: run the LLM and the VTON **concurrently only with a ≤4B model**.
> An 8B LLM (~7GB) + CatVTON (~8GB) ≈ 15GB — works only sequentially (recommend,
> unload, then render). Do not plan SDXL/FLUX + 8B LLM at the same time.

---

## 3. Directory Layout

```
altacloset/
├── PLAN.md                  ← this file
├── README.md                ← quickstart
├── docker-compose.yml       ← 3 services (webapp, comfyui, ollama) + proxy-ready
├── .env.example             ← all config lives here (portability)
├── docs/
│   ├── architecture.md      ← this section expanded + decision log
│   ├── recommender.md       ← wardrobe schema + scoring spec
│   ├── tryon-pipeline.md    ← CatVTON/ComfyUI integration + API contract
│   └── host-202-notes.md    ← verified hardware on the test host
├── data/                    ← gitignored; bind-mounted by services
│   ├── wardrobe/            ← garment images (one per item)
│   ├── uploads/             ← user person photos
│   ├── db/                  ← sqlite wardrobe db
│   ├── comfyui/             ← models/, custom_nodes/, output/
│   └── ollama/              ← model blobs
├── services/
│   ├── webapp/              ← FastAPI app (this is the main code we write)
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   └── app/
│   │       ├── main.py          # routes
│   │       ├── config.py        # reads env
│   │       ├── weather.py       # Open-Meteo + HA override
│   │       ├── wardrobe.py      # sqlite store + seed
│   │       ├── recommender.py   # rule-based scoring engine (MVP core)
│   │       ├── tryon.py         # ComfyUI/CatVTON client
│   │       └── workflows/       # comfyui workflow JSONs
│   └── comfyui/            ← CatVTON install notes / build glue
└── scripts/
    ├── migrate-to-target.sh ← move models + data to the second machine
    └── bootstrap-comfyui.sh ← install CatVTON custom node + weights
```

---

## 4. Phases & Tasks

### Phase 0 — Host prep (202, ~30 min)
- [ ] Confirm GPU/runtime (✅ already verified: driver 580.119.02, toolkit 1.12.1,
      nvidia runtime default, 361G free disk — see `docs/host-202-notes.md`).
- [ ] Pick a home dir for persistent data, e.g. `/opt/altacloset` (or keep in `~/altacloset`).
- [ ] Check 202 RAM headroom — **only ~5.7Gi available right now** (busy box). Verify
      nothing else is fighting for the GPU (`nvidia-smi`) before heavy runs.
- [ ] `git init` + `.gitignore` (`data/`, `*.pyc`, `.env`).

### Phase 1 — Webapp + rule-based recommender (CPU-only, ~1 weekend)
- [ ] **User accounts**: register/login/logout + per-user seed wardrobe (done in skeleton).
- [ ] Wardrobe schema + seed garments (see `docs/recommender.md`).
- [ ] Scoring engine: warmth / formality / color-harmony / occasion / rotation.
- [ ] `GET /api/weather` (Open-Meteo, no API key; optional HA override).
- [ ] `POST /api/recommend` → `{outfit, reasoning}`.
- [ ] Minimal UI: pick activity + prompt → see outfit + reasons.
- **Done when:** can run on CPU with `docker compose up webapp` and get sensible outfits.

### Phase 2 — Try-on: add clothes to a photo (GPU, the wow moment)
Status 2026-08-21: **WORKING end-to-end.** First `/api/tryon` returned a rendered
photo (garment 27 on testdata/person.jpg). See `docs/tryon-pipeline.md` §8 + repo memory.
- [x] Build ComfyUI image (`services/comfyui/Dockerfile`: CUDA 12.8 base + torch cu128 + ComfyUI source + CatVTON release node + detectron2).
- [x] `services/webapp/app/workflows/catvton.json` — API-format workflow; `tryon.py` wires images + cloth_type (top→upper, bottom→lower, dress→overall).
- [x] `scripts/tryon-test.sh` smoke script.
- [x] **Fix SCHP inplace_abn compile** (torch 2.x): the release node's `AugmentCE2P` uses `InPlaceABNSync`, so the **CUDA** kernels are required — the base `runtime` image has **no nvcc** (`/bin/sh: nvcc: not found`). Dockerfile now installs `cuda-nvcc-12-8` + dev headers (cublas/cusparse/cusolver/cudss), patches `z.type()`→`z.scalar_type()` in `inplace_abn_cpu.cpp` (2×) **and** `inplace_abn_cuda.cu` (6×), `ENV TORCH_CUDA_ARCH_LIST=12.0` (Blackwell sm_120), and prebuilds the extension so the `.so` is baked into the image. Verified: 4 CatVTON nodes in `/object_info`, node imports in <1s.
- [x] First try-on (auto-downloads weights ~4-6GB) → `/api/tryon` returns a rendered photo. ~1m26s first run, ~39s warm.
- [ ] Webcam path: browser `getUserMedia` → downscale to ~768–1024px → same endpoint.
- [ ] Background removal for garment images: `rembg` or ComfyUI segment-anything.
- **Done when:** photo of a person in → photo of them "wearing" the recommended top out. ✅ (verified)

### Phase 3 — LLM stylist (optional, phase 2 in evaluation doc)
- [ ] Ollama service with Qwen2.5 3B (or Gemma 3 4B).
- [ ] `POST /api/chat` explains/argues for the pick in natural language.
- [ ] Keeps concurrent with CatVTON (~11.5GB total).

### Phase 4 — Full outfit try-on + polish
- [ ] Multi-garment try-on (top → result → bottom) or 2-garment CatVTON.
- [x] Wardrobe manager: add garments + image upload / product-URL fetch, set/delete images, per-garment serve (2026-08-21).
- [ ] Wardrobe CRUD auto-tagging (auto color/material from image).
- [ ] Daily digest (weather + "wear this today") like Alta's daily outfits.
- [ ] Style calendar / history.

### Phase 5 — Migrate to target machine (see §7)
- [ ] Move compose + data volume with `scripts/migrate-to-target.sh`.
- [ ] Verify identical GPU stack on target; smoke-test all endpoints.

---

## 5. Tech Decisions (locked for MVP)

| Decision | Choice | Why |
|---|---|---|
| Try-on model | **CatVTON** (ComfyUI workflow) | <8GB VRAM @1024×768, garment-image-only input, best quality/VRAM ratio |
| Renderer host | **ComfyUI** (official CUDA image) | CatVTON ships official workflow; easiest packaging |
| Recommender | **Rule-based scoring** first | Zero GPU/latency, explainable; LLM is phase 3 garnish |
| LLM (phase 3) | **Ollama + Qwen2.5 3B / Gemma 3 4B** | ~3–3.5GB, fits concurrent with CatVTON |
| Web framework | **FastAPI** (Python) | Single-language stack with the ML tooling |
| Weather source | **Open-Meteo** (no key) + HA override | Portable across machines, no credentials to move |
| Proxy | Caddy (or none for LAN MVP) | Path to TLS; keep services on 127.0.0.1 |

**Upgrade path (not MVP):** IDM-VTON (SDXL, best realism — but uses the whole 16GB,
run solo as "quality mode") → CatVTON-FLUX / FLUX.1-Kontext (GGUF quantized, ~12–16GB, phase 4+).

---

## 6. API Contract (MVP)

| Endpoint | Method | Body / Params | Returns |
|---|---|---|---|
| `/health` | GET | — | `{ok: true}` |
| `/api/auth/register` | POST | `{username, password}` | `{token, user}` |
| `/api/auth/login` | POST | `{username, password}` | `{token, user}` |
| `/api/auth/logout` | POST | Bearer token | `{ok: true}` |
| `/api/auth/me` | GET | Bearer token | `{user}` |
| `/api/weather` | GET | Bearer token | `{temp_c, feels_like_c, condition, wind_kph, humidity, uv_index}` |
| `/api/wardrobe` | GET | Bearer token | caller's garments (incl. `has_image`) |
| `/api/wardrobe` | POST | Bearer + `{name, category, color?, image_url?}` | created garment (fetches `image_url` if given) |
| `/api/wardrobe/parse-link` | POST | Bearer + `{url}` | `{name, description, color, category, images[]}` from a product page / image link |
| `/api/wardrobe/{id}/image` | POST | Bearer + multipart `image` | updated garment (`has_image`) |
| `/api/wardrobe/{id}/image-url` | POST | Bearer + `{url}` | updated garment (fetches image from URL) |
| `/api/wardrobe/{id}/image` | GET | Bearer token | garment image (owner-only, 404 if none) |
| `/api/wardrobe/{id}` | DELETE | Bearer token | `{ok: true}` |
| `/api/recommend` | POST | Bearer + `{activity, prompt?, weather?}` | `{outfit, reasoning, scores, weather_used}` |
| `/api/tryon` | POST | Bearer + multipart `person`, `garment_id` | `{result_url}` (private, owner-only) |
| `/api/tryon/outfit` | POST | Bearer + multipart | `[result_url]` (Phase 4) |
| `/api/chat` | POST | Bearer + `{message, context?}` | `{reply}` (Phase 3) |

> All data endpoints require `Authorization: Bearer <token>` from register/login.
> Try-on results are served via `GET /api/uploads/{name}` and only to their owner.

Ports (all bound to 127.0.0.1 unless proxied):
webapp `28082`, ComfyUI `28188` (internal only), Ollama `28114` (internal only).

---

## 7. Portability / Migration to the Second Machine

Everything must move via Docker + env; **no host-specific state**.

- [ ] All config in `.env` (paths, ports, weather source, URLs). No absolute paths in code.
- [ ] All mutable data in `data/` bind-mounts / named volume `models` — never baked into images.
- [ ] Pin image tags in compose; bake CatVTON node install into the ComfyUI image build
      (`services/comfyui/Dockerfile`) so the target box needs **zero manual install steps**.
- [ ] Target machine requirements (checklist in `docs/host-202-notes.md`):
      NVIDIA GPU **RTX 5060 Ti 16GB** (or any 16GB+), driver ≥ 535 (580.x verified),
      `nvidia-container-toolkit` ≥ 1.12, Docker ≥ 24 + Compose v2.
- [ ] **Model transfer:** download once on 202, then rsync `data/` (≈6GB CatVTON weights
      + any LLM blobs) to the target — avoids re-downloading. `scripts/migrate-to-target.sh`.
- [ ] Smoke test on target: `docker compose up -d` → `/health` → `/api/weather` →
      `/api/recommend` → `/api/tryon` with a known test image.

---

## 8. Risks & Caveats

| Risk | Mitigation |
|---|---|
| VTON weights are **CC BY-NC-SA 4.0** (non-commercial) | Fine for personal homelab; stop if ever commercializing |
| 16GB = one heavy model at a time | 3–4B LLM + CatVTON concurrent is fine; keep 8B/SDXL sequential |
| 202 is busy (5.7Gi RAM free, possibly GPU-contended) | Run benchmarks off-peak; target machine gets the clean stack |
| CatVTON is SD1.5-era realism | Acceptable MVP; IDM-VTON/FLUX path reserved for phase 4+ |
| Live webcam frames are large | Downscale to ~768–1024px before upload |
| CatVTON tries on **one garment** at a time | MVP = render the top; full outfit = Phase 4 multi-garment |

---

## 9. Definition of Done (MVP)

1. `git clone` + `cp .env.example .env` + `docker compose up -d` works **on 202**.
2. Webapp recommends a sensible outfit for the current weather + a chosen activity.
3. Given a stored or live photo of a person + that outfit's top garment, the app returns
   a believable photo with the garment on the person.
4. The same stack boots **unchanged on the target machine** after `migrate-to-target.sh`.
