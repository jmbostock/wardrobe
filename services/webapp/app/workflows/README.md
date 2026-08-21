# ComfyUI workflows (CatVTON)

`app/tryon.py` loads `catvton.json` from this dir and wires the person/garment
image inputs at request time.

## Phase 2 — add the official workflow here

1. Get the official CatVTON ComfyUI workflow from the repo Releases:
   `https://github.com/Zheng-Chong/CatVTON/releases` → workflow JSON.
2. Save it as **`catvton.json`** in this directory (version-controlled → portable).
3. Fill the node-ID map in `app/tryon.py` → `NODE_IDS`:
   - which node is the `LoadImage` for the **person**
   - which node is the `LoadImage` for the **garment**
   - which node is the **checkpoint/VAE** loader (point it at a model in
     `data/comfyui/models`)
4. Implement `tryon._wire_workflow()` to substitute the uploaded image names +
   checkpoint path.
5. Test: `POST /api/tryon` with a test person photo + a garment that has an
   image at `data/wardrobe/<id>.png`.

Notes:
- ComfyUI must be reachable at `COMFYUI_URL` (default `http://comfyui:8188`).
- The workflow JSON should be a **clean workflow** (API format), not a UI
  workflow — export from ComfyUI with "Save (API format)".
