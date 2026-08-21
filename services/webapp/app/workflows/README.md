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

ComfyUI must be reachable at `COMFYUI_URL` (default `http://comfyui:8188`).
