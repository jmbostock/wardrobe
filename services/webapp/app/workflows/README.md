# ComfyUI workflows (webapp-side)

**API-format** workflows the webapp submits to ComfyUI.

| file | used by | purpose |
|---|---|---|
| `ip2p.json` | `app/editor.py` | InstructPix2Pix — legacy `/api/tryon/edit`. Not wired into any UI. |
| `svd.json` | `app/svd.py` | SVD image → ~3s motion clip. No longer offered on the outfit card. |

## Try-on / refine: built in code, not from a JSON file

`app/tryon.py` builds the Qwen-Image-2.1 graph programmatically (`_qwen_run`)
rather than loading a JSON template. That is deliberate. The graph is small and
fully parameterised (prompt, reference count, `resolution`, seed), and the
historic JSON templates needed the encoder/VAE names patched into BOTH the
subgraph *instance* and the inner node — a step that silently produced a no-op
render when it went wrong.

Two details in that graph are load-bearing, and both carry a comment in the code:

- **`TextEncodeQwenImage21.vae` must be wired** (`["3", 0]`). Without it the
  model silently IGNORES the reference images and hands back the base photo
  unchanged — a no-op that looks like a successful render.
- **`KSampler.latent_image` must be the ENCODER's latent** (`["50", 2]`), not a
  blank `EmptyLatentImage`. A blank canvas makes the model GENERATE a fresh
  person instead of EDITING the real one, which loses identity, pose and framing.

This graph is submitted to **`QWEN_COMFYUI_URL`** (202:8188), a different host
from the legacy `COMFYUI_URL`.

## Removed 2026-09-23

`catvton.json`, `idm_vton.json` and `idm_vton_mask.json` went with the
CatVTON / IDM-VTON stack. Copies are preserved in
`<repo>/.removed-2026-09-23/workflows/` for reference or revert.
