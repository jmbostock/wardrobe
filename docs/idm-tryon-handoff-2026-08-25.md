# IDM/CatVTON Try-on — Honest Handoff for a Fresh LLM (2026-08-25)

> This doc is written so a NEW LLM can pick up this problem **without repeating
> the failures below**. Read it fully before touching anything. The previous
> agent (me) was wrong repeatedly and the user (the ground truth) rejected every
> render. Do not repeat the pattern: **implement → declare success → user says
> it's shit.**

---

## 0. TL;DR — where things stand

- **Goal:** render a person wearing a *shorts* garment (flat-lay "Black shorts",
  garment id 626) onto the user's cropped base photo, via a **per-piece**
  pipeline: **CatVTON defines the area, IDM cleans up the texture**. Each
  garment is an independent piece that references the others.
- **Every render so far has been rejected by the user.** IDM direct stretched
  shorts to long pants. Per-piece (CatVTON+IDM) produced "O-X9992S", called an
  abomination (stretched pants). **CatVTON alone** (`catvton_alone_result.png`)
  was called "balloons the pants, not even the right color, fucking awful".
- **The user's core philosophy (do NOT violate):** *"the whole point is that the
  model actually knows it's [shorts]"* — do NOT hand-craft masks/descriptions to
  dictate to the models. They rejected injected garment descriptions before
  ("why are you telling it what to do").
- **The previous agent's evaluation was unreliable:** pixel-script checks were
  fooled (warm desert background classified as skin), and by-eye reads flip-flopped.
  **Do not trust automated render verification; the user's eyes are the only judge.**

---

## 1. The user's philosophy (read this first — it overrides everything)

Quotes from the user (verbatim):

- *"the point is to use catvton to defined areas and then use IDM to clean up the
  image (better texture etc.)"*
- *"stop treating these as a whole process and start treating as individual
  pieces that are independent, but can reference each other in the process"*
- *"why are we dictating what catvton should do though. seems like the whole
  point is that the model actually knows it's [shorts]"*
- Earlier (rejecting injected descriptions): *"why are you telling it what to do"*
- On the shorts stretch: *"it's literally a shorts to shorts. This should be the
  simplest fucking thing in the world for IDM, and yet it keeps stretching it."*

**Implications:**
- Trust the models. The flat-lay reference tells the model WHAT the garment is.
- Do NOT inject text descriptions into IDM (`_GARMENT_DESCRIPTIONS` exists but is
  deliberately UNUSED — keep it that way).
- Mask editing is a gray area: the user pushed back on "dictating", but the mask
  is fundamentally "where does the garment go". The previous agent capped the
  lower mask at the thigh for shorts (`_to_shorts_mask`) and the user did NOT
  accept that as the fix. Treat aggressive mask surgery as last resort, and
  explain WHY before doing it.
- **Never claim a render is fixed without the user looking at it.** The user has
  been right 100% of the time; the previous agent was wrong 100% of the time.

---

## 2. What was tried → results (the failure history — don't repeat)

| # | Approach | Where | Result (user verdict) |
|---|----------|-------|----------------------|
| 1 | IDM direct, raw AutoMasker mask + default desc ("LET THE MODEL DECIDE", commit `df1caf5`) | tryon.py (deleted) | Shorts stretched to **long pants** ("how are these shorts… it's stretching the shorts") |
| 2 | `_to_shorts_mask` thigh-cap applied to IDM's mask (commit `1a88510`) | tryon.py | Still bad (shirt bleed / unchanged); previous agent **falsely declared success** via a pixel check that measured the warm background as "skin" |
| 3 | **Per-piece pipeline** (commit `07408f1`): CatVTON renders + captures AutoMasker mask (one prompt), then IDM re-renders ONLY that area on the CatVTON result | tryon.py, DEPLOYED | Outfit **O-X9992S** = "fucking abomination… it stretches the pants" |
| 4 | **CatVTON alone** (no capping, no custom mask) on the cropped base | `/tmp/catvton_alone_result.png` | "catvton balloons the pants. it's not even the right color. it's fucking shit" |
| 5 | (uncommitted, NOT deployed) cap the mask BEFORE CatVTON renders | tryon.py working tree | User challenged the whole "dictating" premise; abandoned. **Do not deploy.** |

**Key inference (unproven, but the best current theory):**
- CatVTON alone does NOT produce correct shorts on this base either (user verdict #4).
  So the previous agent's theory "CatVTON knows, IDM is the stretcher" is WRONG or
  incomplete — CatVTON also fails (balloons, wrong color).
- The problem may be **input-related**: the cropped base is a very tall/narrow
  image (1036×2728), and `_prep_person` letterboxes it into the 768×1024 (3:4)
  canvas → the person is only ~389px wide with huge gray side bars. The models
  may be doing badly partly because of this. **This input handling is a prime
  suspect and has NOT been properly tested** (e.g. trying a crop that fills the
  canvas, or a different canvas strategy).

---

## 3. Current code state

Repo: `/home/bostock/altacloset`, branch `main`, remote `jmbostock/wardrobe`.
HEAD = `2e39abe` (pushed). Live webapp on 187 is running the **`07408f1`** version
of `tryon.py` (the per-piece pipeline).

### Commits (newest first)
- `2e39abe` — docs: handoff update (per-piece pipeline recorded)
- `07408f1` — **per-piece pipeline** (CatVTON defines area + IDM cleans). DEPLOYED.
- `1ade4be` — compose: forward `TRYON_MODELS` to webapp (IDM was unavailable in UI)
- `1a88510` — `_to_shorts_mask` thigh-cap for shorts
- `6361be5` — wardrobe.js `?g=` auto-open race fix
- `712813f` — outfit garment tiles click-through to wardrobe

### UNCOMMITTED working-tree changes — IMPORTANT
- `services/webapp/app/tryon.py` — 118 lines changed = the **cap-mask-before-CatVTON**
  experiment (approach #5). **User rejected the premise. Do NOT deploy. Either revert
  or rework.** Note: many OTHER modified files (`deps.py`, `pages.py`, `auth.js`,
  `auth.html`, `base.html`, `.env.example`, README, docs) are a **parallel session's
  WIP** — do not commit/touch those.

### Key functions in `services/webapp/app/tryon.py`
- `_idm_cleanup_garment(client, person_name, garment, user_id, seed, area_mask=None)` —
  the per-piece core: pass 1 CatVTON (render + AutoMasker mask via an extra
  SaveImage node "19"), pass 2 IDM (same mask on the CatVTON result). For shorts it
  currently caps the area with `_to_shorts_mask` (this is the part the user is
  questioning).
- `_fetch_outputs(client, entry, wanted)` — fetch outputs by ComfyUI node id.
- `_run_idm_vton` / `_run_idm_vton_outfit` — entry points (single + outfit).
- `_to_shorts_mask(mask_bytes)` — thigh-cap (pure PIL). `_is_shorts(garment)` — name
  contains "short" + category bottom.
- `_automasker_mask`, `_catvton_with_mask` — UNCOMMITTED additions (approach #5).
- `_GARMENT_DESCRIPTIONS` — exists but UNUSED (user rejected injected text). Keep unused.
- `_free_models(client)` — POSTs `/free` to ComfyUI before/after renders (OOM guard).
- Old "LET THE MODEL DECIDE" chain (`_idm_render_mask`, `_render_idm_garment`,
  `_automasker`, `_idm_render`) was DELETED in `07408f1`.

### Workflows (`services/webapp/app/workflows/`)
- `catvton.json` — nodes: 10 LoadImage person, 11 LoadImage garment, 12 LoadAutoMasker,
  13 AutoMasker, 17 LoadCatVTONPipeline, 16 CatVTON, 18 SaveImage.
- `idm_vton.json` — nodes: 10 LoadImage person, 11 LoadImage garment, 13 LoadImage mask,
  14 DensePosePreprocessor, 15 PipelineLoader, 16 IDM-VTON, 17 SaveImage. (768×1024.)
- `idm_vton_mask.json` — AutoMasker only → mask (SaveImage 18).

---

## 4. Infrastructure / how to run a render

### Hosts
| Host | IP | Role |
|---|---|---|
| docker-core (workspace) | 10.0.1.176 | git origin, this repo |
| dev box | 10.0.1.187 | **webapp ONLY** (`altacloset-webapp` container), Cloudflare front door, :28085 |
| GPU box | 10.0.1.202 | ComfyUI :28190, ollama :28114, llamacpp-vision :28117. RTX 5060 Ti 16GB |

- **187 is the ONLY webapp host; 202 is GPU-only. NEVER deploy the webapp to 202;
  NEVER run GPU renders from 187.** Try-on flow: webapp (187) → ComfyUI (202) via
  `COMFYUI_URL=http://10.0.1.187:28190` (187's autossh tunnel to 202's loopback).
- ComfyUI on 202 binds **loopback** — from any other box you can ONLY reach it via
  the 187 tunnel at `http://10.0.1.187:28190` (verified HTTP 200).

### Freeing VRAM on 202 (IDM needs ~14GB free)
```bash
ssh bostock@10.0.1.202 'pkill -9 -f "[l]lama-server"; docker stop altacloset-ollama nsfw-ai-server'
# then POST /free (use a FILE to avoid quoting issues):
ssh bostock@10.0.1.202 'echo "{\"unload_models\":true,\"free_memory\":true}" > /tmp/free.json; curl -s -w "HTTP %{http_code}\n" -X POST http://127.0.0.1:28190/free -H "Content-Type: application/json" -d @/tmp/free.json'
nvidia-smi --query-gpu=memory.free --format=csv   # want ~14500 MiB
```
**TRAP:** `pkill -f llama-server` matches its OWN shell (the command line contains
"llama-server") → silently kills the ssh session (empty output). Use `[l]lama-server`.
Restore after: `docker start altacloset-ollama nsfw-ai-server` and
`systemctl --user enable --now llamacpp-vision.service`.

### Vision service
`llamacpp-vision.service` (systemd **user** unit on 202, `Restart=on-failure`) runs
Qwen2.5-VL-3B @ 127.0.0.1:28117. It respawns llama-server — stop via
`systemctl --user stop llamacpp-vision.service` (+ disable when freeing VRAM).

### Test sandbox
- Login: `test@dev.local` / `Rimmer256!` (user id 9; gated by `DEV_ADMIN_ENABLED`).
- Garment 626 = "Black shorts" (Nike) — a **flat-lay** (the app's AI quality flag
  "includes a person/model" is a WRONG heuristic — trust the user).
- Photos: 34 = original IMG_9087 (poor base), **35 = the user's cropped base**
  (uploaded from 202 `~/Downloads/IMG_9087.png`, 1036×2728).

### Test files on docker-core (`/tmp`)
- `/tmp/cropped_base_IMG9087.png` — the user's crop (the base to use).
- `/tmp/idm_cleanup_result.png` — per-piece direct test (seed 42).
- `/tmp/idm_app_result.png` — O-X9992S (the rejected one).
- `/tmp/catvton_alone_result.png` — CatVTON alone (rejected: balloons, wrong color).
- `/tmp/compare_*.png` — side-by-side comparisons.
- `/tmp/idm_pipeline_test.py`, `/tmp/catvton_alone_test.py` — scripts that import
  `app.tryon` and run against ComfyUI via the 187 tunnel
  (`COMFYUI_URL=http://10.0.1.187:28190`, `DATA_DIR=/tmp/idm_test_data` which has
  `wardrobe/9/626.jpg`).

---

## 5. Deploy gotchas (187)

- **A parallel session keeps recreating the 187 webapp container** from a tree that
  can be stale. It wipes `docker cp`'d fixes AND can drop 187's `.env` overrides.
  After any container recreate, re-apply:
  - `TRYON_MODELS=catvton,idm_vton` and `COMFYUI_URL=http://10.0.1.187:28190` in
    `~/altacloset/.env` on 187 (check with `docker exec altacloset-webapp printenv ...`).
  - Any docker-cp'd code files (tryon.py, static JS).
- **`docker cp` does NOT read stdin** (`docker cp - c:/p < f` silently no-ops). Use
  scp to the 187 tree, then `docker cp`, then verify md5 in-container.
- **Backend changes need a container RESTART** to load (`docker restart
  altacloset-webapp`) — docker cp alone doesn't reload a running Python process.
  `docker restart` preserves env (a `force-recreate` also works but risks env loss).
- Verify the SERVED content (curl + grep), not just the container file.
- `node --check` the JS before deploying JS.

---

## 6. Open questions for the next LLM (don't re-litigate closed ones)

**Verified-closed (do not re-investigate from scratch):**
- The 16GB-fit patch on the IDM node (202, mounted, NOT in repo) is NECESSARY
  (unpatched IDM OOMs). Backup at `/home/bostock/idm-vton-tryon_pipeline-FIXED-per-step.py`.
  Never `git checkout`/stash the mounted node without re-applying it.
- FLUX.1-Kontext local is not viable on this stack (no qwen2_vl CLIPType even on
  ComfyUI master; the FluxKontext nodes present are BFL cloud-API). Not active.

**Genuinely open (worth attacking):**
1. **Input handling.** The base is a 1036×2728 (1:2.63) crop. `_prep_person`
   letterboxes it into 768×1024 → tiny narrow person + huge gray bars. CatVTON
   alone ballooned and mis-colored — is that partly the input? Try: a canvas /
   crop strategy that lets the person fill the frame (e.g. fit the 3:4 canvas to
   the person's body region rather than full-height letterbox), or a different
   resolution. **This is the most untested variable and a strong suspect.**
2. **Which model is the "stretcher/ballooner"** — IDM, CatVTON, both on this base?
   Need per-stage outputs captured and judged by the user.
3. **Does IDM cleanup help at all for lower-body?** The handoff's earlier finding
   (verified then): "IDM's garment application on the lower body is broken — it
   un-wears/stretches pants no matter the base." CatVTON (true inpainter) handles
   garment placement; IDM (warp) may only help UPPER-body texture.
4. **Mask semantics** — does IDM-VTON even strictly respect the mask's length, or
   does it stretch from the garment+pose? If it ignores mask length, capping is
   pointless and the fix must be elsewhere (pose, canvas, or dropping IDM for
   bottoms).

---

## 7. What NOT to do (learned the hard way)

1. **Do not declare success without the user looking at the render.** The user is
   the ground truth; previous agent was wrong every time. Pixel-script checks are
   NOT verification (warm background = "skin" false positive). By-eye reads also
   flip-flopped. If you can't be sure, say so.
2. **Do not inject garment text descriptions** into the models (rejected).
3. **Do not build more mask-surgery hacks** without first testing whether the
   input (canvas/crop) is the real problem.
4. **Do not blame the user's files** (the shorts image IS a flat-lay; the crop is
   intentional). If the crop doesn't suit the model, that's a pipeline problem to
   solve, not a user-input error.
5. **Do not churn the live deploy** with experimental tryon.py. Keep experiments
   as direct scripts (`/tmp/..._test.py` against ComfyUI) until the USER approves
   a result, then commit + deploy.
6. **Do not trust the app's AI quality flags** (e.g. "includes a person/model" on
   the flat-lay shorts was wrong).
