# ComfyUI workflows (CatVTON)

`catvton.json` is the **API-format** CatVTON workflow built from the official
UI workflow (release tag `ComfyUI`). `app/tryon.py` wires it per-request:

- uploads person + garment via ComfyUI `/upload/image`
- sets `cloth_type` from the garment category (top/outerwear → `upper`,
  bottom → `lower`, dress → `overall`)
- submits to `/prompt`, polls `/history/{id}`, returns the rendered image

Node ids used by `tryon.NODE_IDS`:

| role | node | class |
|---|---|---|
| person_image | 10 | LoadImage |
| garment_image | 11 | LoadImage |
| masker_pipe | 12 | LoadAutoMasker |
| automasker | 13 | AutoMasker |
| tryon_pipe | 17 | LoadCatVTONPipeline |
| catvton | 16 | CatVTON |
| output | 18 | SaveImage |

First run downloads the weights automatically from HuggingFace
(SD1.5-inpainting + `zhengchong/CatVTON` DensePose/SCHP/attention checkpoints) —
takes a while on first try-on.

## SVD (motion clip)

`svd.json` is the **API-format** SVD image-to-video workflow that animates a
try-on still into a ~3s clip. `app/svd.py` drives it:

- letterboxes the still onto the 576x1024 SVD canvas (aspect-preserving)
- submits to `/prompt` and returns immediately — ComfyUI queues the job, so
  several clips / a clip + a try-on can run back-to-back without blocking
- `check_svd()` polls `/history/{id}`; the webapp saves the animated WEBP to
  uploads and attaches it to the outfit (`motion_url`)

Model: `svd_xt.safetensors` (Stable Video Diffusion XT, 25 frames). Download it
into `/root/.cache/huggingface/svd/` and symlink it to
`/opt/ComfyUI/models/checkpoints/svd_xt.safetensors` (same pattern as IP2P).

Node ids used by `svd.NODE_IDS`: image=2, sampler=5.

ComfyUI must be reachable at `COMFYUI_URL` (default `http://comfyui:8188`).
