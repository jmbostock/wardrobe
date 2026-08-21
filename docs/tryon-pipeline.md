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
  release zip. detectron2/DensePose is now **GPU-compiled** (`FORCE_CUDA=1`,
  sm_120): the original layer built it CPU-only (`FORCE_CUDA=0`, no nvcc back
  then) and a later layer reinstalls it with CUDA once nvcc is present, so
  DensePose's ROIAlign/NMS/deform kernels run on the GPU.
  The `comfyanonymous/comfyui` Docker Hub image does NOT exist — build from source.
- **WORKING end-to-end, GPU-bound.** `altacloset-comfyui` healthy on host port
  **28190** (internal `comfyui:8188`). All 4 CatVTON nodes load
  (`LoadCatVTONPipeline/LoadAutoMasker/CatVTON/AutoMasker` in `/object_info`).
  First `/api/tryon` returned a rendered photo (~1m26s incl. weight download,
  ~39s warm). **GPU utilization hits 100% during inference** (DensePose + SCHP +
  CatVTON), peak VRAM ~6.3GB / 16GB. Weights auto-download on first try-on into
  `./data/comfyui/models`.
- **SCHP inplace_abn fix (the blocker):** the release node's SCHP network
  (`networks/AugmentCE2P.py`) uses `InPlaceABNSync`, so the extension's **CUDA**
  kernels are required at runtime — a CPU-only build is NOT enough. Two issues:
  1. Base image is the CUDA **runtime** variant → **no nvcc** → `.cu` targets
     died with `/bin/sh: 1: /usr/local/cuda/bin/nvcc: not found`.
  2. Extension is torch 1.x code: `AT_DISPATCH_FLOATING_TYPES(z.type(), ...)`
     in `inplace_abn_cpu.cpp` (2×) **and** `inplace_abn_cuda.cu` (6×) fails in
     torch 2.x (`cannot convert DeprecatedTypeProperties to ScalarType`).
  The Dockerfile now (a) installs `cuda-nvcc-12-8` + dev headers (cublas/
  cusparse/cusolver/cudss — pulled in by torch's `ATen/cuda/CUDAContextLight.h`),
  (b) patches `z.type()`→`z.scalar_type()` (also `.type().scalarType()` in the
  wrapper and `.type().is_cuda()` in `checks.h`), (c) sets
  `ENV TORCH_CUDA_ARCH_LIST=12.0` (Blackwell sm_120, keeps the prebuilt extension
  cache key stable), (d) prebuilds the extension so the `.so` is baked into the
  image (node imports in <1s on first boot).
- Build gotchas already fixed in the Dockerfile: re-pin torch/torchvision/torchaudio
  to cu128 (ComfyUI reqs upgrade to cu13 → `libcudart.so.13` missing); upgrade
  transformers to `>=4.44,<5` (node's 4.27.3 pin breaks ComfyUI Qwen2 nodes);
  `python3-dev` for detectron2; `--no-build-isolation` for `pip install -e detectron2`.
- Version pins: `libcublas-12-8` is a **held package** in the base image
  (`--allow-change-held-packages` needed); `cudss.h` ships under
  `/usr/include/libcudss/12/` → symlinked into `/usr/local/cuda/include`.

## 7. Phase 4 — full outfit

Option A: chain (top on person → result → bottom on result). Simple, works, degrades slightly.
Option B: multi-garment CatVTON variants / ComfyUI multi-garment workflows.
Upgrade path for quality: IDM-VTON (SDXL, uses whole 16GB — run solo) →
CatVTON-FLUX / FLUX.1-Kontext (GGUF ~12–16GB, ~30–90s/img).
