# Clueless Closet — what *you* can do to make it better

> Written 2026-09-23, after the Qwen-Image-2.1 migration and the de-backgrounded
> wardrobe release (v0.45.0). This is a **user checklist**, not a roadmap — the
> roadmap (what's next in the *code*) lives in `docs/product.md` §7.
>
> Everything here is ordered by how much it improves the app per minute of your
> time. Items 1–5 are the high-leverage ones.

---

## 1. Give feedback on recommendations (biggest payoff, lowest effort)

Every recommendation card has **👍 / 👎**. Both are recorded in the interaction
log and feed the taste learner, so they change what Cher suggests next.

- **👍** = "more like this" (weight `+2`).
- **👎** opens a reason menu. **The reason is the valuable part** — each one
  steers a *different* style dimension:

  | reason | what it teaches |
  |---|---|
  | Not my style | down-weight this whole style cluster |
  | Wrong color | colour preference (feeds the avoid list) |
  | Bad pattern/print | pattern preference |
  | Too formal / Too casual | shifts your formality band |
  | Doesn't fit me | fit, not taste |
  | Don't like this item | this garment specifically |
  | **Just not today** | *nothing* — deliberately transient |

- **Thumbs-down is safe.** Negative signals decay with a **14-day** half-life
  while positive ones last a **year**, so a 👎 for "too formal today" doesn't
  permanently bury a good item. Use **Just not today** when it's weather or mood
  rather than the clothes.
- Feedback is also **per-occasion**: a dislike logged against "office" counts
  less when Cher is dressing you for a date.

## 2. Rate things (one tap, strongest per-item signal)

- **Garment cards** → open the detail card → rating slider. **7–10** logs a
  strong positive (`rated_up`, weight `+4`); **1–3** logs a negative
  (`rated_down`, `−2`). 4–6 is deliberately neutral.
- **Outfit cards** (Outfits tab) have the same slider — rating an outfit logs the
  rating against **every garment in it**, so one rating teaches several items at
  once.
- Rating also drives the **Top rated** sorting in the Wardrobe.

## 3. Try things on, and refine instead of re-rendering

- Every render logs `tried_on` (`+2`) for each garment; **saving** an outfit logs
  `saved` (`+3`). Both are stronger than a thumbs-up.
- When a look is *almost* right, use **✨ Refine this outfit** rather than
  starting over: describe the change ("make the top long-sleeved", "turn her to
  the side", "warmer light"). It saves a **new** outfit and never overwrites the
  original, so you keep both to compare.
- Rendering is ~60–120 s per pass (and 3-item outfits chain into several passes),
  so it's worth a batch session rather than one-at-a-time.

## 4. Fill in the Style profile (Account → Style profile)

Optional, but it's what makes the *first* recommendations sensible instead of
generic, and it also acts as a guardrail:

- **Sizes** (top / bottom / shoe) — used for the family fit check on shared clothes.
- **Warmth bias** (runs cold / hot) — shifts layering.
- **Formality min–max** — the band Cher stays inside.
- **Never wear** — becomes a **hard guardrail** (`no_shorts`, `no_dresses`,
  `no_tank`, `no_sandals`, …): those garments are excluded, not just down-ranked.
- **Style keywords**, **favourite / avoid colours**, **typical week** — these are
  the taste priors.
- Height/body build feed the fit reasoning for shared items.

## 5. Audit what the AI filled in on each garment

Upload pre-fills brand/colour/category/sizes from a vision read, and it **is
sometimes confidently wrong** (a near-black blazer described as *"navy blue with
silver trim"*; a dark-grey sweater tagged *"Navy crewneck"*; those wrong
descriptions then leaked into renders as invented trim and wrong colours). Check,
in this order:

1. **Category** — wrong category puts a garment in the wrong slot entirely.
2. **Colour** — the single biggest input to outfit scoring and colour harmony.
3. **Fit** (Regular / Baggy / Tight) — this decides **which base photo** gets
   used to try it on; a baggy base photo is actively penalised for tight
   garments, so a wrong fit = a wrong-looking render.
4. **Name / brand / size** — cosmetic, but the name is what you'll recognise in
   pickers.

Also worth doing once: hunt **near-duplicates** (cards flagged `⚠ similar to X`)
and keep the better photo, since duplicates split the learning signal.

## 6. Person / base photos are half of try-on quality

- Keep **2–3 good full-body bases** (Account → My photos). Between them they
  should cover both a **fitted** and a **loose/casual** outfit.
- **Write a description on each one** — this is not decoration. The description
  is the fit signal the base picker matches against the garment ("fitted jeans +
  tucked tee, fitted" / "relaxed tee + baggy joggers, loose").
- Prefer a **clean background** (green screen, or an AI-cleaned cutout); busy
  scenes leak into the render.
- Heed the **suitability chip** — a red/low-score base is a bad try-on input; the
  auto-pick prefers high scorers but a bad photo in the set still costs you.
- Mark the **default** photo deliberately.

## 7. Get the real wardrobes de-backgrounded (only the test sandbox has this)

The clean cutouts you're looking at exist for the **test sandbox only** (163
garments). Your real wardrobes still show the flat-lay photos.

- Batch job: `bg_remove.py` on **202**, ~**60 s per garment** → 163 garments ≈
  **2.7 hours**. Run it overnight; 202's GPU is needed, so the vision/LLM
  services have to be free.
- It writes **new** `<gid>.cutout.png` files — every original is kept, so a bad
  cutout costs nothing but a re-check.
- Worth spot-checking ~10 of them afterwards (garments on hangers, sheer or
  very light-coloured fabrics, and anything shot on a patterned surface are the
  likely misses).
- The **wardrobe display already handles both cases**: cutout where one exists,
  original photo where it doesn't, so a partial run is fine.

## 8. Let the learning actually switch on (it isn't instant)

Two ML layers sit behind the rule-based scorer, and both have thresholds:

| layer | turns on when | where it trains |
|---|---|---|
| Style centroid (FashionCLIP) | **≥3** distinct *engaged* garments — engaged = tried on / saved / rated / liked, **not** merely shown | `scripts/rec_build.py` (embeddings) |
| ALS collaborative model | **≥10** real (non-`shown`) interactions | `scripts/rec_weekly.sh` |

- Both are **batch-trained**, not live: `rec_weekly.sh` runs weekly (systemd user
  timer on 202) and pushes the model + embedding back to 187.
- So: after a solid feedback session, **run `scripts/rec_weekly.sh`** manually if
  you want to see the effect before the next weekly run. Feedback given today
  does not move today's suggestions.

## 9. Testing worth doing on the sandbox (not live)

Use the **`test`** dev account — it's a sandbox copy, so mistakes cost nothing:

- **3+ garment outfits** — pass scheduling renders lowers first, then uppers, and
  never mixes them in one pass; confirm nothing gets dropped (blazers are the
  usual casualty).
- **Refine prompts** — identity/scene are held, pose is deliberately free: try
  "turn her to the side", "make the top long-sleeved", "change the background to
  a café".
- **Click-through** — from an outfit's garment tile into the wardrobe item.
- **Phone check** — after this release the service-worker cache is `closet-v42`;
  if a tile looks stale on the phone, close and reopen the PWA.
- **Cutout quality** — sanity-check the de-backgrounded cards look right at
  grid size (that's the size that matters) before agreeing to run the batch for
  the real wardrobes.

## 10. Loose ends you may want to decide on

- **Motion clips (SVD, ~9 GB)** and **InstructPix2Pix (~7 GB)** are still
  installed on 202 but no longer reachable from the wardrobe/outfits UI. ~16 GB
  reclaimable if you're happy to drop them — the try-on tab still has a clip path.
- **Test suite**: two tests are stale from the Qwen migration and currently fail.
  Neither is user-facing, but a red suite hides the next real regression:
  - `test_wardrobe.py::test_rotate_180_flips_and_stays_portrait` still assumes the
    saved photo comes back 1200 px tall (it is downscaled) → `IndexError` reading
    row 1100.
  - `test_sharing.py::test_interactions_logged` expects `recommender.recommend()`
    to write a `shown` impression row. It no longer does —
    `interactions.log_outfit_shown()` is now **never called from anywhere**.
    Impact today is nil (`shown` has weight 0.5 and is explicitly *excluded* from
    the style centroid and from the ALS interaction gate), but the impression log
    was meant to be the fuel for future negative sampling, so either re-wire the
    call where the outfit is actually rendered/shown, or delete the helper and
    the test deliberately. Don't leave it ambiguous.
