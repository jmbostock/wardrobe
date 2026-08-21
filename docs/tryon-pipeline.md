# altacloset — Try-On Pipeline (CatVTON via ComfyUI)

Goal: take a **person photo** + a **garment image**, return a photo of the person
"wearing" the garment. MVP renders the recommended **top**; full outfit is Phase 4.

## 1. Why CatVTON

- **VRAM: <8GB @ 1024×768 (bf16)** — official figure; comfortable on a 5060 Ti 16GB.
- SD1.5-based → fast on consumer GPUs (~10–20s/image on the 5060 Ti).
- Input is **garment image + person photo only**; person mask + garment mask are
  auto-generated (DensePose + SCHP). No reference-model photo needed.
- Ships an **official ComfyUI workflow** (in repo Releases) + Gradio app.
- License: CC BY-NC-SA 4.0 — fine for personal/homelab, non-commercial only.

Links:
- Repo: https://github.com/Zheng-Chong/CatVTON
- ComfyUI workflow: repo Releases → `workflow/`
- Mask-free variant: `zhengchong/CatVTON-MaskFree`

## 2. Pipeline

```
person photo (from upload OR webcam)
   │  downscale to ≤1024px  (docs: don't upscale; 768–1024 typical)
   ▼
garment image (data/wardrobe/<id>.png)
   │  background removal → rembg (CPU) or ComfyUI segment-anything node
   ▼
ComfyUI — CatVTON workflow (catvton.json)
   │  POST /prompt  → workflow_id
   │  poll GET /history/{id}  → outputs
   ▼
rendered photo  → saved to data/uploads/out/  → URL to webapp
```

## 3. ComfyUI integration (client sketch)

```python
# services/webapp/app/tryon.py
async def run_tryon(person_bytes, garment_bytes) -> bytes:
    # 1. upload person + garment via ComfyUI /upload/image
    # 2. load workflows/catvton.json, substitute image nodes + ckpt path
    # 3. POST /prompt -> {prompt_id}
    # 4. poll GET /history/{prompt_id} until "status.completed" (timeout ~120s)
    # 5. fetch the output image from /view
```

Key notes:
- ComfyUI is **internal only** (`comfyui:8188`, not published to LAN in compose).
- Workflow JSON is versioned in `services/webapp/app/workflows/` so it's portable.
- If ComfyUI is unavailable (e.g. CPU-only dev), `/api/tryon` returns `503` with a
  clear message rather than hanging.

## 4. Webcam path (browser, no server-side camera)

```js
const stream = await navigator.mediaDevices.getUserMedia({video: {facingMode: "user"}});
// capture a frame to <canvas>, downscale to max 1024px, toBlob('image/jpeg')
// POST multipart {person: blob, garment_id} -> /api/tryon
```

## 5. Background removal (garment images)

- `rembg` (u2net, CPU) is simplest and portable.
- Or ComfyUI `segment_anything`/`briaai` nodes if we want to keep it in the GPU app.
- Store the **clean cutout** next to the garment so try-on input is always mask-friendly.

## 6. Expected perf on 5060 Ti 16GB (30 steps, ~1024×768)

| Step | Time |
|---|---|
| CatVTON inference | ~10–20s |
| rembg background removal | ~1–3s |
| Total per garment | ~15–25s |

## 8. Deployment reality + current status (2026-08-21)

- ComfyUI is **built from source** (`services/comfyui/Dockerfile`): base
  `nvidia/cuda:12.8.1-cudnn-runtime-ubuntu22.04` + torch cu128 (Blackwell sm_120
  needs cu128+), ComfyUI from `Comfy-Org/ComfyUI`, CatVTON node from the official
  release zip, detectron2/DensePose compiled CPU-only (`FORCE_CUDA=0`).
  The `comfyanonymous/comfyui` Docker Hub image does NOT exist — build from source.
- Running on 202: `altacloset-comfyui` healthy on host port **28190** (internal
  `comfyui:8188`). CatVTON nodes currently FAIL to load: SCHP `modules`
  `inplace_abn` extension uses torch 1.x API
  (`inplace_abn_cpu.cpp:89/107`, `z.type()` → removed `DeprecatedTypeProperties`).
  Fix: `sed 's/z\.type()/z.scalar_type()/g'` on those 2 lines + rebuild.
- Build gotchas already fixed in the Dockerfile: re-pin torch/torchvision/torchaudio
  to cu128 (ComfyUI reqs upgrade to cu13 → `libcudart.so.13` missing); upgrade
  transformers to `>=4.44,<5` (node's 4.27.3 pin breaks ComfyUI Qwen2 nodes);
  `python3-dev` for detectron2; `--no-build-isolation` for `pip install -e detectron2`.

## 7. Phase 4 — full outfit

Option A: chain (top on person → result → bottom on result). Simple, works, degrades slightly.
Option B: multi-garment CatVTON variants / ComfyUI multi-garment workflows.
Upgrade path for quality: IDM-VTON (SDXL, uses whole 16GB — run solo) →
CatVTON-FLUX / FLUX.1-Kontext (GGUF ~12–16GB, ~30–90s/img).
