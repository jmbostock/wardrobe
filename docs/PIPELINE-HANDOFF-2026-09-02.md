# Altacloset Try-On Pipeline — Debug Handoff (2026-09-02)

A self-contained write-up of the virtual-try-on ("Clueless Closet" / altacloset) rendering
pipeline, its full fix history, and the ONE open problem currently blocking good results
(garment color fidelity). Written so another engineer/LLM can take over without prior context.

---

## 1. System overview

| Thing | Detail |
|---|---|
| App | "Clueless Closet" (altacloset) — self-hosted virtual try-on (put clothes on a photo of a person) |
| Webapp | FastAPI, Docker. Runs on **187** (10.0.1.187), container `altacloset-webapp`, host port `28085` → internal `8000`. SQLite at `/data/db/altacloset.db` (host: `~/altacloset/data/db/altacloset.db`) |
| GPU box | **202** (10.0.1.202), RTX 5060 Ti **16GB**. Runs: ComfyUI `altacloset-comfyui` (`:28190`), llama.cpp vision (`llamacpp-vision`) |
| Tunnel | 187→202 via autossh: `28190` (ComfyUI) and `28117` (vision) |
| Users | `7` = Melissa (real). `9` = test sandbox (`test@dev.local` / `Rimmer256!`), a copy of 7 — all test renders use user 9 |
| Repo | `~/altacloset` (services/webapp/...). Remote `github.com/jmbostock/wardrobe` (loosely synced) |

**Vision on-demand (added 2026-09-02):** vision no longer runs 24/7. A tiny stdlib proxy
`vision-proxy.py` runs on 202 listening on `127.0.0.1:28117` (what the tunnel/webapp targets).
On request it starts `llamacpp-vision` (now bound to `:28217`), waits for the model, proxies
through; it stops the service after ~5 min idle (frees ~4GB VRAM for IDM). Vision call timeouts
in the webapp were raised 10/40s → 150s to cover the ~60s cold start.

**Deploy method (critical gotcha):** scp file → `bostock@10.0.1.187:altacloset/services/webapp/app/...`
(EXACT sub-path) → `docker cp` into `/app/app/...` → `docker restart altacloset-webapp` → verify
`import`. Multi-file `scp` to `.../app/../` flattens files and leaves the container on STALE code
(hit this repeatedly). Verify md5/behavior after every deploy.

---

## 2. The canonical pipeline (single path, no model selection)

All rendering goes through the IDM-VTON pipeline. CatVTON = geometry/placement owner, IDM = texture,
with deterministic post-processing for face/background/color guarantees. Entry:

- Outfit (multi-garment): `tryon._run_idm_vton_outfit(person_bytes, garments, user_id)` (`services/webapp/app/tryon.py`)
- Single garment: `tryon._run_idm_vton(...)` (NOT yet color-matched — see §6)

Route `POST /api/tryon/outfit` (`routes/tryon_routes.py`) always calls the outfit pipeline (no `models`
param since 2026-09-02). Results auto-save to the Outfits page.

### Phase 0 — capture masks ONCE from the ORIGINAL bare base (order-independent)
- Lower garment (`CLOTH_TYPE == "lower"`) mask first → `_to_shorts_mask()` or `_to_pants_mask()` →
  returns `(mask_bytes, waist_row_fraction)`.
- Upper/outerwear garments → AutoMasker `"upper"` mask → `_to_top_mask(raw, lower_waist)` (trim to waist).
- Outerwear (`category == "outerwear"`) is dilated first via `_expand_mask(pct=0.10)` so a jacket covers
  the shoulders/arms (was rendering too small).
- Rationale: masks from the bare base (not the running composite) remove order-dependence and the
  "shorts mask covers the shirt" / "tee bleeds into shorts" bugs.

### Phase 1 — CatVTON composite
`for g in canon:` where `canon` = uppers/outerwear first, bottoms last (canonical layering). Each garment
painted via `_catvton_with_mask(client, pn, gn, mask_name, seed)` using the pre-captured mask — CatVTON's
own AutoMasker is never run on the running composite.

### Phase 2 — IDM re-textures each garment INDEPENDENTLY
`for g in canon:` each pass runs IDM on the **ORIGINAL bare base** (never the running composite — that
was the source of cross-garment color bleed, e.g. navy bomber → green joggers), with the garment's own
mask + its own `garment_clean` image. Result clipped to its mask via `_composite_masked(...)`. Face
restored after every pass.

### Post-processing guarantees
- **Clothes-only:** `union = _merge_masks(all garment masks)` then `_composite_masked(current, person_bytes,
  union)` — everything OUTSIDE the garment union is the original base photo, pixel-identical (walls, face,
  background, exposed skin never change).
- **Face never touched:** `_restore_face(render, base)` pastes the ORIGINAL base photo's face band back
  after every CatVTON and IDM pass. The face band is located by HSV skin-tone detection
  (`_detect_face_top`) because the base photos have headroom (face sits at ~10-21% of frame height, NOT
  the old fixed top-14%). Restores [0, face_top + 8-11%h], central column only (28-72% width) so
  side-hair is not dragged over shoulders.
- **True garment color (NEW, partial — see §6):** after the final composite, for each garment,
  `_reference_garment_color(clean_image)` measures the garment's actual mean color (non-white pixels),
  and `_match_garment_color(output, mask, ref)` shifts that region so its average color == the garment's
  actual color (shading preserved).

Key helpers in `tryon.py`: `_to_shorts_mask`, `_to_pants_mask`, `_to_top_mask`, `_expand_mask`,
`_automasker_mask`, `_catvton_with_mask`, `_idm_cleanup_garment`, `_idm_garment_bytes`, `_composite_masked`,
`_merge_masks`, `_restore_face`, `_detect_face_top`, `_is_skin_px`, `_reference_garment_color`,
`_match_garment_color`, `_GARMENT_DESCRIPTIONS`, `CLOTH_TYPE`.

IDM prompt = STORED `garments.vision_desc` when present (computed at upload/nightly via vision); else a
category fallback. **The fallback for pants used to literally say "a pair of pants (jeans)"** — fixed to
never say jeans (denim bias).

Clean/background-removed garment: `media.remove_garment_background` → stored as `<gid>.clean.png` next to
the garment (written at save, backfilled). IDM textures from the CLEAN image (blank background) so the
flat-lay background has zero influence; CatVTON uses the raw flat-lay (geometry only).

---

## 3. Hard user rules (do NOT violate)

1. **Only the clothes change.** Background, walls, face, neck, exposed skin must be pixel-identical to the
   source photo.
2. **Never touch the face.** Ever.
3. **The garment color must be the garment's ACTUAL color.** The base photo's clothing/scene color must
   have ZERO influence. "it should just start with white and paste the actual color on top." (This is the
   current open bug.)
4. **IDM stays in its lane**: paints only inside what CatVTON found (its mask); never bleeds; never
   figures out geometry; a separate, independent process per garment.
5. **Flat-lay background has zero influence** on IDM (hence background-removed clean images).
6. **Input order must not matter** (canonical layering internally).
7. **One pipeline only** — no "choose model" UI (removed 2026-09-02).
8. **Required services start on demand** (vision wake proxy; not 24/7).
9. Never send raw image URLs to the user — point to the Outfits page. User is the only judge of visuals;
   do not claim image quality/color from your own viewing.

---

## 4. Fix history (chronological, abbreviated)

- Base picker: garment/base type via vision classification + hard gate (dress→dress base etc.). Auto-pick is
  now the DEFAULT dropdown option; server picks a compatible base.
- Masks order-dependence: masks were computed mid-pipeline on the running composite → moved to capture once
  from the bare base.
- CatVTON composite order-dependence: was running its own AutoMasker on the running composite → now uses the
  pre-captured bare-base masks for every garment.
- Cross-garment bleed (navy bomber colored green joggers in O-ZRQHH7/O-YHVZQ4): caused by feeding IDM the
  running composite (it saw the bomber). Fixed by running every IDM pass on the bare base + `_composite_masked`.
- Background/wall drift: CatVTON re-generates the whole frame → final "clothes-only" union composite pastes
  the original base everywhere outside the garment union.
- Face drift: `_protect_face` (zero top band of the mask) was insufficient (IDM re-generates whole frame);
  added hard `_restore_face` from the original base, located by detected face position.
- Hair-over-bomber artifact: full-width face band dragged side-hair over shoulders → narrowed to central
  column.
- Garment not brown (flare pants): (a) empty vision_desc → generic prompt even said "jeans" (fixed, backfilled
  322 garments + 34 photos), (b) IDM scene-harmonization darkens garment vs bright flat-lay → color crushed
  to near the base's original dark pants.

---

## 5. Current open problem — garment COLOR fidelity

**Symptom:** Brown flare pants (garment 504, user 9) render near-black / too dark / patchy, never the true brown.

**Measured data (mechanical, not judged visually):**
- Reference (clean image `504.clean.png`) garment color ≈ **RGB (126, 98, 75)** (full-image avg incl. white bg
  = 174,156,141). Brown.
- Base photo 50 ("Sleeveless shirt and pants") — the person is **already wearing dark pants** ≈ (38,36,35).
- Renders of look [484 navy peplum top, 504 brown flare pants, 561 beige blazer] on base 50:
  - O-C8GEJ3 (before any color work): pants ≈ (43,41,39) — near-black, ≈ base's original pants.
  - O-MBW3JY (after correct vision descs + removing "jeans" prompt): ≈ (88/61/43) — dark.
  - O-LGHF6Y (after neutralizing the IDM context region): ≈ (89/89/60) + **a visible hazy box** around the
    garment (neutralization artifact) — slightly better but still too dark.
  - O-6UDBQ8 (after deterministic true-color match): right leg (134,106,85) ≈ **true brown** ✓, but left leg
    still (89,83,73) dark → **patchy**.

**What we proved:**
1. The deterministic color-match works when applied to pixels inside the mask (right leg hit the true brown).
2. It fails where the rendered garment lies OUTSIDE the AutoMasker-derived mask — so the color-match (and the
   clothes-only composite, and every other mask-clip) leaves those pixels untouched → the base's original
   dark pants / IDM's dark paint shows through → patchy.

**Root cause (likely the real blocker for ALL remaining artifacts):**
The AutoMasker mask region and where the generative models (IDM/CatVTON) actually paint the garment do NOT
align. Masks are computed from the bare base's original clothing silhouette; CatVTON/IDM then generate the
garment with their own geometry offset/expanded from that. Every mask-based operation (composite, face-safe,
color-match, union) is therefore applied to the wrong pixels, producing: base-color peek-through, haze,
patchiness, uneven color.

**Directions to investigate (not yet implemented):**
1. Find the ACTUAL painted garment region in the output (segmentation of the rendered garment pixels in the
   final render, e.g., via the garment-vs-base color/silhouette or a re-run segmentation) and do color/composite
   operations on THAT region instead of the AutoMasker mask.
2. Or, make the mask used for IDM/color-compositing deliberately larger/closed so it fully contains where the
   models paint (dilate/close, fill the pants two-leg center gap) — a uniform, generous region for color-match
   and the clothes-only union.
3. Consider whether per-garment independent rendering on the bare base is worth the color/harmonization cost vs.
   rendering each garment on a version of the base where ITS region is neutral/blank — but note the DensePose
   body-pose dependency and the haze-box failure of the earlier neutralize approach.

**Other known gaps:**
- The single-garment path `_run_idm_vton` does NOT yet have the true-color match (needs its garment region mask).
- IDM/CatVTON harmonize garment color to scene lighting (bright studio flat-lays get darkened to fit indoor
  photos); this is why purely generative color fails and why a deterministic color-correct step is required.

---

## 6. Files to touch / where to look

- `services/webapp/app/tryon.py` — the whole pipeline + helpers (main work happens here).
- `services/webapp/app/tryon_routes.py` — `POST /api/tryon/outfit` (single pipeline), base-pick gate.
- `services/webapp/app/media.py` — `remove_garment_background`, clean-image write.
- `services/webapp/app/vision_cache.py` — refresh garment/photo descriptions (vision, on-demand).
- `services/webapp/app/photopick.py`, `aifill.py` — other vision consumers (timeouts raised for cold start).
- `services/webapp/app/static/js/tryon.js` + `templates/tryon.html` — UI (single result, Auto-pick base default).
- 202: `/home/bostock/bin/vision-proxy.py`, `~/.config/systemd/user/{vision-proxy,llamacpp-vision}.service`.

Test account: `test@dev.local` / `Rimmer256!` (user 9). To re-run the failing look: POST
`/api/tryon/outfit` with `garment_ids=[484,504,561]`, `photo_id=50`. Keep the GPU free first (stop
`llamacpp-vision`, ComfyUI `/free`).
