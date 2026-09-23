# cluelesscloset — Try-On Pipeline (Qwen-Image-2.1)

Goal: take a **person photo** + one or more **garment images**, and return a photo
of that person wearing those clothes — without changing their face, their body,
or the room they are standing in.

**Current renderer: Qwen-Image-2.1 image-edit.** The CatVTON + IDM-VTON
inpainting stack was removed on 2026-09-23 (see §7).

---

## 1. Why Qwen-Image-2.1

- **It is a general image editor, not a try-on network.** The person is passed as
  `<image1>` and each garment as its own reference image, so one edit pass covers
  a whole outfit. There is no mask stage, no DensePose, no per-piece geometry
  pass, and therefore none of the failure modes those stages introduced.
- **It holds the garment reference better.** CatVTON/IDM needed a mask to know
  *where* a garment goes, then a texture pass to make it look right, then
  face-restore and colour-correction repairs on top to undo the damage. Qwen
  gets colour, sleeve length and printed graphics right from the reference alone.
- **It is promptable.** The same renderer takes a free-text instruction, which is
  what powers **Refine this outfit** (§6). CatVTON could not do this at all — it
  had no text conditioning for the garment, and its own description field had to
  be deliberately left empty (it was actively harmful; see §4).
- **Native 2048², up to 10 reference images.**
- License: **Qwen Research** — check it suits your use before shipping anything.

It replaced, in order of how well each worked: CatVTON (geometry right, texture
poor) → CatVTON-geometry + IDM-texture (better texture, but IDM re-generated the
whole frame, so it drifted backgrounds, recoloured garments and needed a face
restore to stay honest).

## 2. Pipeline

```
person photo (saved photo / upload / saved-outfit render)
   │  uploaded to ComfyUI as qwen_person.png
   ▼
garment images (data/wardrobe/<owner>/<id>.clean.png — garment on plain white)
   │  uploaded as qwen_ref_2.png, qwen_ref_3.png, …
   ▼
Qwen-Image-2.1 edit pass  (app/tryon.py::_qwen_run)
   │  TextEncodeQwenImage21 → KSampler → VAEDecode → SaveImage
   │  POST /prompt → prompt_id;  poll GET /history/{id}
   ▼
rendered PNG → data/uploads/<user>/out/ → served to the webapp
```

Outfits of **3+ garments are split into several passes** (see §5).

## 3. Hosts and configuration

| what | where | setting |
|---|---|---|
| Webapp | 187 (container `cluelesscloset-webapp`) | `http://10.0.1.187:28085` |
| Qwen renderer | 202:8188 (container `qwen-comfy`) | `QWEN_COMFYUI_URL` |
| Legacy ComfyUI URL | — | `COMFYUI_URL` — still read by `svd.py` / `editor.py` only |

The Qwen instance needs **ComfyUI ≥ 0.37 + ComfyUI-GGUF** (`qwen-image-2.1-Q4_K_M.gguf`).
It is deliberately *not* managed by `docker-compose.yml`: it is a separate,
independently-versioned install on the GPU host (`~/qwen-image/` on 202).

## 4. The one rule that matters: never describe a garment in the prompt

`app/tryon.py::_qwen_edit_prompt` names each reference by **role only** —
*"the item shown in `<image3>` as the outer layer"*. It never says what the
garment looks like, because **an appearance claim in the prompt overrides the
reference image**.

Both available metadata fields are unreliable, and both were caught in
production:

| injected | what happened |
|---|---|
| `vision_desc` | The near-*black* blazer #539 described as *"navy blue with silver trim"* → the model drew silver trim along the lapels and hem that the blazer does not have (outfits 114/115). |
| `g.name` | *"Navy crewneck"* on a **dark grey** sweater, *"Navy blazer"* on that black blazer → **both** garments turned navy; the sweater lost its correct colour (outfit 118). |
| nothing (roles only) | Sweater dark grey ✓, blazer near-black ✓, no invented trim ✓. |

Two related traps, both verified the hard way:

- **Never feed an RGBA cutout.** The model reads the **alpha channel as fabric**
  and renders semi-transparent clothing (outfit 115, `O-WFJ4MR`). Use
  `<gid>.clean.png` — the garment on plain white.
- **`cfg` is 1.0, so negative prompts are inert.** Proven: same seed and refs,
  the only change being a negative prompt, gave **zero differing pixels**. Keep
  everything positive, and do not add "no collage / no side-by-side" — that
  measurably backfired.

### The three files a garment has, and who is allowed to see which

A garment photo has two derived companions, and picking the wrong one is how
both the purple wardrobe and the semi-transparent jacket happened:

| file | what it is | who uses it |
|---|---|---|
| `<gid>.jpg/png` | the original photo, flat-lay backdrop and all | **the truth** — rotate, re-upload, phash/colour, vision, embeddings |
| `<gid>.clean.png` | garment on plain white | **the renderer's reference** (`_garment_reference_bytes`) |
| `<gid>.cutout.png` | true RGBA cutout, background removed | **the UI only** (`media.garment_display_path`) — never the renderer |

So a request to "use the de-backgrounded image" means **the UI**, not the
try-on reference: the cutout stays display-only until an RGBA reference is
proven safe. See `docs/architecture.md` #26 for the serving rules.

## 5. Pass scheduling (why 3 items is not one pass)

A single pass with 3 references **collapses**: the model blends or silently drops
one. Two references is the reliable ceiling. But re-chaining alone is not enough
— the *order* decides whether the last pass survives. Verified 3-way at a fixed
seed (base 53, crewneck + jeans + navy blazer):

| schedule | result |
|---|---|
| single pass, 3 refs | 2/3 — blazer bled into the sleeves |
| `[jeans]` → `[crewneck, blazer]` | **3/3 ✓** |
| `[crewneck, jeans]` → `[blazer]` | **blazer dropped** |

Rule (`_qwen_passes`): group into passes of ≤2, **lowers first, then uppers, and
never mix the two within a pass**. Each category group is chunked separately.

## 6. Refine this outfit

The Outfits page card offers **Refine this outfit**: a text box plus a button,
which calls `POST /api/outfits/{id}/refine` → `tryon.refine_render()`.

- The render goes in as `<image1>` with **no garment references** — the clothes
  are already on the person, so the model only has to follow the instruction.
- **Nothing is overwritten.** The result is saved as a *new* outfit carrying the
  same garments, so the original and the refinement sit side by side and either
  can be refined again. Renders are permanent artifacts.
- One pass takes ~60–120s; the button shows a live timer.

The prompt holds **identity and scene** ("identical face, hair, skin tone and
body", "same background, lighting and framing") but deliberately does **not** pin
the pose — saying "identical pose" would actively fight a request like *"turn her
to the side"*.

Refine replaced the old **Make a 3s clip** button on that card.

## 7. Removed 2026-09-23 — CatVTON / IDM-VTON

Removed: `workflows/catvton.json`, `workflows/idm_vton.json`,
`workflows/idm_vton_mask.json`, `services/comfyui/`, `scripts/bootstrap-comfyui.sh`,
the `comfyui` compose service, and every mask/face-restore/colour-match helper in
`tryon.py` (`tryon.py` went from 1861 → 707 lines). **~33 GB** of CatVTON and
IDM-VTON weights were deleted from 202.

Two things changed as a *consequence*, and both are improvements:

- **Base-photo style classification is now vision-based.** It used to run
  CatVTON's AutoMasker and read the `lower` mask's start height plus a bare-leg
  skin test — meaning the GPU renderer had to be online just to answer "is this
  person wearing a dress?", and it returned `unknown` whenever ComfyUI was busy.
  `classify_person_style` now asks the vision model directly.
- **An `unknown` base no longer blocks a render.** The base-matching gate used to
  treat `unknown` as a mismatch and refuse; it now only refuses on a *positive*
  mismatch. Refusing good photos because the classifier was unsure was wrong.

Preserved for reference in `<repo>/.removed-2026-09-23/`. The dated handoffs in
`docs/` (`idm-tryon-handoff-2026-08-25.md`, `PIPELINE-HANDOFF-2026-09-02.md`) are
kept as historical records of that era, not as current documentation.

**Still installed, deliberately NOT wired to any UI (2026-09-23):** Stable Video
Diffusion motion clips (`svd.py` + `workflows/svd.json`, ~9 GB of weights on 202)
and InstructPix2Pix (`editor.py` + `workflows/ip2p.json`, ~7 GB). Motion clips are
**on hold, not removed** — the user is still deciding what to do with them, so the
try-on "Make a 3s clip" control and the clip display on the outfit cards were
removed, while `POST /api/tryon/clip`, the `clips` table and every clip already
rendered stay exactly as they are (nothing is ever deleted). Re-exposing it is a
button plus a `submitClip()`; do not treat the resulting dead code as a bug.

## 8. Operational notes

- **Treat `QWEN_MODELS["clip"]` as the encoder switch.** It currently points at
  `qwen3vl_8b_w4a8_heretic.safetensors`, an abliterated build of the Qwen3-VL-8B
  text encoder. Dropping in the stock `qwen3vl_8b_w4a8.safetensors` is a
  one-string change; nothing else moves.
- **The text encoder is the only place "refusal" lives.** It is a language model
  and the largest single component (~6.3 GB); the DiT has no refusal mechanism
  (it just hits a capability ceiling) and the VAE is a pure codec. No external
  safety filter sits in this pipeline.
- **Two graph details are load-bearing** and are commented in `_qwen_run`:
  `TextEncodeQwenImage21.vae` must be wired (without it the model silently
  returns the base photo unchanged), and `KSampler.latent_image` must be the
  encoder's latent (a blank canvas makes it *generate* a new person).
- `resolution` is a **total pixel budget**, not a width. It is set to `1024`.
- 202's GPU is shared — check `/queue` before queueing a test run.
- If the renderer is unreachable, `/api/tryon*` returns **503** with a clear
  message rather than hanging.
