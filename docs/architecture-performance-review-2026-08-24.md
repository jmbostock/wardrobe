# Clueless Closet — Architecture & Performance Review (2026-08-24)

Full review of the recommendation pipeline, data layer, and the reported UI
slowness. Includes: (1) verdict on the previous LLM's review, (2) measured
baseline, (3) the real root causes of perceived slowness, (4) your two specific
concerns (quick edits → UI, wardrobe icons → edit page), (5) recommendation
pipeline review, (6) data storage/collection review, (7) prioritized action plan.

---

## 0. TL;DR — "is the app fast enough?"

**The backend is fast. The frontend image-loading strategy is the bottleneck, and
none of it is Cloudflare bandwidth.**

Measured on 187 (2026-08-24, in-network):

| Call | Latency | Notes |
|---|---|---|
| `GET /api/wardrobe` (all 189 garments) | **30–40 ms** | 9.8 KB JSON. O(N²) queries but SQLite is quick at this scale |
| One full-res garment image | ~8 ms (in-net), 71 KB | but many are 250–600 KB (full camera res) |
| `POST /api/recommend` | **~730 ms** | **~710 ms of that is the external Open-Meteo weather call**, fetched fresh every time |
| `GET /api/weather` | ~710 ms | external, **uncached** |
| Interactions logged | 147 rows | ML layers (style/ALS) are currently **data-starved** |

So a single round-trip is fast. The slowness you and family feel comes from:

1. **Every image is re-downloaded on every page load** (`Cache-Control: no-store`
   + `fetch(cache:'no-store')` + a `?v=Date.now()` cache-buster that changes every
   single render). Nothing is ever reused — not by the browser, not by Cloudflare,
   not by the PWA.
2. **Images load serially** — `loadWardrobe()` `await`s each full-res image *inside
   the loop* before appending the next card. 40 garments = 40 sequential round-trips
   (each with a ~250–600 KB full-res body), and the grid populates one card at a time.
3. **No thumbnails** — camera-res photos (250 KB–1.5 MB) are served straight into
   ~150×200 px UI cards.
4. **A ~700 ms weather fetch blocks every recommendation** (`/api/recommend`,
   `/api/suggest`, first chat message).

On your LAN (you're on `10.0.1.187:28085` directly) this is "slow-ish but usable".
For anyone on the public Cloudflare tunnel (your wife, external), RTT + the
uncompressed full-res re-downloads make it feel much worse — **but the fix is
caching + thumbnails + parallelism, not more bandwidth.** Cloudflare tunnel
bandwidth is fine; it's the *number and size* of uncached requests that hurts.

---

## 1. Verdict on the previous LLM's review

| Prior finding | Verdict | Notes |
|---|---|---|
| **1. `no-store` headers cause re-downloads** | ✅ **Correct — root cause #1** | `wardrobe_routes.py:235`, `photos_routes.py:103` set `Cache-Control: no-store`; `common.js:authImageUrl()` also passes `cache:'no-store'`; `wardrobe.js:gimg()` adds `?v=Date.now()`. The design is "never cache, ever," by construction. |
| **2. Full-res payloads for tiny cards** | ✅ **Correct — root cause #2** | `media.save_garment_image()` writes the original bytes (orientation-normalized, JPEG q88) with **no downscale and no thumbnail**. 189 images on disk are 250–600 KB. |
| **3. VLM calls "on dropdowns" hang UI** | ⚠️ **Right problem, wrong page** | The 10 s `best_photo_for_garment` VLM call is **not** on the wardrobe edit dropdown — it's on the **Try-on page's garment picker** (`tryon.js:autoPickBestPhoto()`, fires on every `.look-select` change, no debounce, and `fast=true` exists but is never passed). **It is NOT the cause of your "wardrobe icons → edit page slow" complaint.** It *is* a real annoyance on the Try-on tab. |
| **4. GPU VRAM swapping (CatVTON + SVD)** | ✅ **Real, but not your interactive-UI problem** | ~8 GB CatVTON + ~9 GB SVD on 16 GB is genuinely over the ceiling. You already mitigated it (DeepSeek cloud chat, `OLLAMA_KEEP_ALIVE=2m`). It only bites when try-on and clip runs overlap — a batch/queue concern, not the page-feels-slow concern. |

| Prior next step | Verdict |
|---|---|
| **Fix image caching + versioning** | ✅ Agree — but the versioning must be **content/mtime-derived and stable**, not `Date.now()`, and edits must bump it so your quick edits still show up instantly. |
| **Default dropdowns to `fast=True` heuristic** | ✅ Agree — and also debounce + memoize the VLM pick so it runs once, not per change. |
| **Auto WebP thumbnails on upload** | ✅ Agree — highest-leverage single change for grid speed. |

The prior review's biggest miss: it never identified **serial `await` image loading**
and **full-grid re-render on every edit** — which is exactly what you feel when you
"make quick edits" and when you "click wardrobe icons." That's §3A and §4 below.

---

## 2. Your two specific concerns

### 2a. "I make quick edits — I don't want to jump through hoops to see them"

Current behavior when you edit a garment (rename/rating/color/upload/rotate):

- The detail sheet PATCHes, then calls **`loadWardrobe()` again** — which
  re-fetches the full list AND serially re-downloads **every** garment image with
  `no-store`. Your one tiny edit triggers 40 network fetches. That's the "hoop."
- For rotate/upload the image fetch is correct (fresh), but it's a **full-res**
  no-store download.

Why it *feels* like you have to "jump through hoops":

- **Static assets** (`app.css`, JS) are served `no-cache` (good — normal browsers
  pick up your changes on reload), **but the service worker is cache-first**, so
  **PWA/iPhone users need the `sw.js` `CACHE` constant bumped on every release** to
  see your JS/CSS changes — that's the manual hoop baked into the deploy.
- **Images** are never cached, so edits always "show" but slowly.

What "showing up" should mean after an edit:
- **Edit one card in place (optimistic UI)** — update the DOM row, don't re-render
  the whole grid and don't re-fetch other garments' images.
- **Versioned immutable image URLs** — `/api/wardrobe/{id}/image?v=<mtime|hash>`.
  `Cache-Control: max-age=86400, immutable`. The browser/CDN cache the old version
  hard; when you edit, the `v` changes → the URL changes → the browser fetches the
  fresh one. **You get real caching AND instant edit visibility** — the two things
  you asked for, without the `no-store` trade-off.
- **Automate the SW bump** — derive the `CACHE` name from a build/commit marker so
  you never hand-edit `sw.js` (or drop JS/CSS from SW precache and let the
  `no-cache` HTTP layer handle freshness).

### 2b. "Clicking wardrobe icons → edit page not fast enough"

The detail sheet itself opens **instantly** (it's client-side DOM). What's slow is:

1. **The grid took forever to appear** (serial full-res fetches), so the whole page
   *feels* slow before you even tap.
2. The detail image is another full-res `no-store` download, so the photo in the
   sheet pops in late (blank color placeholder meanwhile).

Fix (same as 2a): thumbnails + parallel lazy loading for the grid; versioned
cached URLs so the detail image is already in cache from the grid.

---

## 3. Root causes of perceived slowness, ranked

### A. Serial, no-store, full-res image loading — THE dominant issue
`wardrobe.js:45` `loadWardrobe()`:
```js
for (const g of items) {
  ...
  const url = await authImageUrl(gimg(g.id));   // await INSIDE the loop
  img.src = url;
  ...
  grid.appendChild(card);
}
```
- `await` in a loop = **strictly serial** downloads.
- `authImageUrl` = `fetch(cache:'no-store')` + `URL.createObjectURL(blob)` — never
  cached, and **blob URLs are never revoked** (memory leak: each reload/relogin
  leaks ~40 blob objects of 250–600 KB each on a phone).
- `gimg(id)` appends `?v=Date.now()` — changes every call, so **even the HTTP cache
  is defeated by construction**, and it defeats any future `max-age` you add.
- Same pattern in `outfits.js` (grid + detail), `suggest.js` chat cards, `tryon.js`
  pickers, `account.js`. **Every image in the app** goes through this.

### B. No thumbnails
`save_garment_image` never resizes. Camera photos stay 250 KB–1.5 MB. A 300 px WebP
would be ~15–40 KB — **10–20× smaller** with no visible difference in a 150×200 card.

### C. Weather fetched fresh on every request (~700 ms)
`weather.fetch()` hits Open-Meteo on every `/api/weather`, `/api/recommend`, and
`/api/suggest` (and chat when `weather_ctx` is absent). Weather doesn't change every
second. A **5-minute TTL cache** (in-process) drops recommend latency ~730 ms → ~30 ms.

### D. VLM photo-pick on every Try-on garment change (up to 10 s)
`tryon.js:autoPickBestPhoto()` fires on every `.look-select` change (and every
`checkGarmentImage()`), calling `/api/photos/best-for-garment/{id}` **without**
`fast=true` → a multi-image `qwen2.5vl` call with a 10 s timeout and model-load
latency. The `fast=true` pure-PIL path (<2 ms) exists but is never used from the UI.
Debounce it and run the AI path once per garment (memoized), or default to `fast`.

### E. Wardrobe list is O(N²) in DB queries
`GET /api/wardrobe`:
- 1 query for `wardrobe.all()`, then per garment:
  - `garment_dict()` → `nearest_dup()` → `near_duplicates()` → **another full
    `wardrobe.all()` scan** (N extra full-table queries)
  - `sharing.state()` → **one more query** (N queries)
- ~2N+2 queries per list call, all under one global lock. At 189 garments it's still
  only ~40 ms, but it grows quadratically to 300+ garments and is trivially fixable
  with two batched queries (fetch all phashes once; fetch all `user_garment_state`
  rows for the user once).

### F. Single SQLite connection + one global lock, no WAL
`db.py` uses one connection + a `threading.Lock`, and **never sets
`PRAGMA journal_mode=WAL`**. Every read and write serializes through one lock (the
FastAPI threadpool contends), and a write locks out reads. SQLite defaults to
rollback-journal mode, which blocks readers during writes. **WAL + a small pool (or
`check_same_thread` read connections)** removes most contention. `interactions`
writes on every recommend (`shown` per garment) add write pressure.

### G. Blob URL leak
`authImageUrl()` never calls `URL.revokeObjectURL()`. On a phone PWA that re-renders
the wardrobe on every edit, this accumulates real memory. Use `<img>` with `src` set
from a **revoked-after-load** blob, or better — stop using blobs and use versioned
URLs straight through the browser cache (§2a).

### H. Static-asset freshness is a manual hoop
`sw.js` precaches JS/CSS and serves **cache-first**. For PWA/iPhone users, a deploy
of new JS/CSS is invisible until someone bumps `const CACHE` in `sw.js` (the hoop
you're complaining about) — and then often needs **two** visits (SW installs on
first, claims on second). The `no-cache` HTTP header only helps non-PWA browsers.
Fix: network-first for navigation, stale-while-revalidate for assets, and derive
the cache name from the served version (auto-bump), or don't SW-precache JS/CSS at
all.

---

## 4. Recommendation pipeline — how you're getting recommendations

**Design is sound. Current data makes it inert.**

### Layer 1 — rules (`recommender.py`) ✅ good
Deterministic, explainable, ~20 ms (minus weather). Warmth/formality/occasion/
waterproof/harmony/rotation/prompt all wired. Two small gaps:
- `rating`, `created_at`/recency, `last_worn`, and **pair-history from rated
  outfits** are still **not** used as scorer terms (the rec-engine-v2 plan's
  "Layer-1 enhancements" were never implemented). `rotation_bonus` only uses
  `wear_count`.
- `user_garment_state` (per-person fit/rating/wear) feeds the fit filter but not
  scoring bonuses.

### Layer 2 — FashionCLIP style centroid (`embeddings.py`) ✅ correct, tiny data
- Weighted, time-decayed centroid from engaged garments; correct use of `shown`
  exclusion and half-life. Requires ≥3 engaged garments — fine.
- **Request-time cost**: `user_style_vector()` runs a full `interactions` JOIN query
  + one `get_vector` per candidate **on every recommend()**. At 189 garments it's
  ms-level, but cache the vector per (user, day) to keep it flat as data grows.
- `learner.load_model()` does `np.load` of the ALS factors **on every request** —
  cheap now, but worth a process-level cache.

### Layer 3 — ALS (`learner.py`) ✅ right tool, but starved
- 147 interactions total (130 `shown`, 12 `rated_up`, 3 `tried_on`, 2 `liked`) —
  far below `MIN_INTERACTIONS=10` for most meaningful signal, and `shown` (weight
  0.5) dominates the log as noise. The ALS term is effectively dormant.
- **Verdict:** the fusion is correct and safe (inert until data), but until the
  family actually uses thumbs-up/down + try-on + "did you wear it", Layers 2/3 add
  ~nothing. **That's fine** — they're the growth path, not the current bottleneck.

### Recommendation-to-UI latency
The **~700 ms weather fetch dominates every recommend/suggest/chat** (§3C) — that is
the single biggest "recommendations feel slow" fix, bigger than any ML tuning.

---

## 5. Data storage & collection

### Storage
- **SQLite, single file** — correct for this scale (few users, ~300 garments/user).
  The schema is clean and additive (garments/outfits/photos/interactions/
  user_garment_state/garment_embeddings/chat_sessions).
- **Concerns:** (F) no WAL + one global lock; embeddings as BLOBs is fine but the
  `garment_embeddings` table needs an index-friendly approach if you ever query by
  vector (you don't — you scan all — OK at this scale).
- **Images on disk, full-res, no thumbnails** — see §3A/B. Consider a `thumb/`
  sibling file or a `_thumb.webp` per garment, generated at save time (cheap PIL),
  invalidated when the image changes.

### Collection
- **`interactions` logging is well-designed** (`interactions.py`): kinds + weights +
  context JSON; logged on shown/tried_on/saved/rated/liked/disliked/worn.
- **One weakness:** `shown` is logged inside `recommender.recommend()` on *every*
  recommend — including every `POST /api/recommend`, every suggest, and the
  recommend-inside-chat. That's fine (it's the point of an impression log), but at
  scale it's the majority of rows and should be **periodically downsampled** (keep
  the last N `shown` per garment) so it doesn't bloat the ALS matrix.
- Family sharing model is flat and correct (single "Family" group, opt-in share,
  per-person `fit_ok`). `sharing.state()` per-garment in the list route is the only
  N+1 smell (§3E).

---

## 6. Prioritized action plan (what to change, in order)

**P0 — frontend image path (fixes your two complaints + most slowness):**
1. Generate **~300–400 px WebP thumbnails** on garment save/upload; serve those in
   grids + chat cards; keep full-res only for the detail sheet + try-on.
2. Serve garment/photo images with **`Cache-Control: public, max-age=86400,
   immutable`** and **versioned URLs** (`?v=<file mtime or content hash>`). Edits
   (rotate/upload) rewrite the file → new `v` → new URL → fresh fetch. Delete
   `?v=Date.now()` and the `cache:'no-store'` fetch; use a plain `<img>`.
3. **Parallel + lazy**: replace the `await`-in-loop with `Promise.all` (or set
   `src` and let the browser parallelize) + `loading="lazy"` + a stable URL. No more
   one-card-at-a-time.
4. **Optimistic, in-place edits**: on save/rotate/upload, update the one card's DOM
   + the in-memory item; bump only that image's `v`. Drop the full `loadWardrobe()`
   re-render on every edit.
5. **Revoke blob URLs** if you keep `authImageUrl` anywhere (or eliminate it).

**P1 — backend latency:**
6. **Cache weather ~5 min** in-process (TTL keyed on lat/lon/source). Recommend goes
   ~730 ms → ~30 ms.
7. **Batched wardrobe list**: one query for all phashes (precompute dup map) + one
   query for all `user_garment_state` rows; kill the per-garment `sharing.state()`
   and `near_duplicates()` full scans.
8. **Enable WAL** (`PRAGMA journal_mode=WAL` in `db.init()`) — removes read/write
   serialization with zero code change beyond the PRAGMA + a small pool or
   connection-per-thread-read.

**P2 — UX hoops:**
9. Try-on picker: **debounce `autoPickBestPhoto`** and **default `fast=true`** for
   interactive updates; run the VLM pick once per garment (memoize) or on an
   explicit button.
10. **Auto-bump the SW cache** from `version.py`/commit so you stop hand-editing
    `sw.js` (or make static SW serve stale-while-revalidate instead of cache-first).
11. Add the Layer-1 scorer terms that are already planned (rating/recency/
    last_worn/pair-history) — cheap, improves cold-start recommendations now.
12. Downsample `shown` interactions periodically.

---

## 7. What's already good (don't touch)
- Rule-based Layer 1: fast, explainable, zero-GPU.
- DeepSeek cloud for chat (frees VRAM), `OLLAMA_KEEP_ALIVE` VRAM discipline.
- Deterministic orientation + near-dup (center-crop dHash + canonical color gate).
- Interaction log + family sharing: correct data model for the stated goal.
- Graceful degradation everywhere (AI fill, photo pick, vision) — never blocks the
  app *hard*; the try-on picker is the one place it gets close.
- Versioned, additive migrations and per-page thin routers — clean to extend.

**Bottom line:** the architecture is the right shape for a small family wardrobe;
the app is fast on the backend and slow in the browser because it re-downloads
full-res images serially with caching disabled. Fix the image path (P0) + cache
weather (P1) and the "app is slow" complaints should largely disappear without
touching the recommendation or storage design.
