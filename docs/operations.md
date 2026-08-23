# Clueless Closet — Operations

Operational notes for the running system (post-transition, 2026-08-23).
Architecture/spec: `docs/recommendation-engine-v2.md`.

## Host roles

| Host | Role | What runs |
|------|------|-----------|
| **187** (10.0.1.187) | **PRIMARY** — webapp + data | webapp (docker compose), data at `~/altacloset/data` (db + wardrobe + uploads) |
| **202** (10.0.1.202) | **GPU-only compute** | llama.cpp vision `qwen2.5vl` :28117 (user unit `llamacpp-vision.service`), ComfyUI :28190, FashionCLIP embedding, ALS training |

Models live on the shared NVMe at `/mnt/models/altacloset/` (vision/ + rec/hf), per the
homelab model-storage pattern.

## The weekly learning loop (automated)

`scripts/rec_weekly.sh` runs on **202** via systemd user timer **`rec-retrain.timer`**
(every **Sun 03:00**, linger enabled). It:

1. **Pull** — snapshots 187's live DB (SQLite online backup; webapp stays up) and
   copies any new `wardrobe`/`uploads` images to 202.
2. **Build** — runs `scripts/rec_build.py` (FashionCLIP embed of new garments +
   implicit ALS retrain). ALS auto-skips below 20 non-'shown' interactions.
3. **Push** — upserts fresh `garment_embeddings` rows into 187's DB (never loses
   interactions that landed mid-run) and copies `data/rec/als.npz` to 187.

Needs **202→187 passwordless SSH** (202's ed25519 key is in 187's
`~/.ssh/authorized_keys`). Log: `202:~/altacloset/logs/rec_weekly.log`.

Manual run / status:

```bash
ssh bostock@10.0.1.202 'bash ~/altacloset/scripts/rec_weekly.sh'          # run now
ssh bostock@10.0.1.202 'systemctl --user list-timers rec-retrain'          # next fire
ssh bostock@10.0.1.202 'tail -20 ~/altacloset/logs/rec_weekly.log'         # last run
```

## Vision (AI tag-reading on upload)

- 187 webapp → `VISION_ENGINE=llamacpp`, `VISION_URL=http://10.0.1.187:28117`
  (in 187's `.env`) → autossh tunnel → 202 `qwen2.5vl` :28117.
- The tunnel is a **manual autossh** process, NOT a systemd unit:

  ```bash
  autossh -M 0 -N -L 0.0.0.0:28117:127.0.0.1:28117 -L 0.0.0.0:28190:127.0.0.1:28190 bostock@10.0.1.202
  ```

  **Caveat:** it won't come back after a 187 reboot — restart it manually (or
  convert to a systemd unit). `gpu-tunnel.service` is not active.

## Batch ingest

```bash
ssh bostock@10.0.1.202 'cd ~/altacloset && \
  VISION_ENGINE=llamacpp VISION_URL=http://127.0.0.1:28117 \
  DATA_DIR=$HOME/altacloset/data \
  /usr/bin/python3 scripts/ingest_batch.py "<photos dir>" --email <user>'
```

Photos are HEIC→JPEG + downscaled to ≤1024px before the vision model reads them.

## Data & current state

- Source of truth: **187** `~/altacloset/data`. 202's copy is a compute mirror
  refreshed by the weekly loop.
- State (2026-08-23): v0.36.0 · 196 garments (170 = mazarrag/user 7) · 193 FashionCLIP
  embeddings · 0 interactions → L2 (style vector, ≥3 engaged) and L3 (ALS, ≥10 + model)
  are latent until the family engages with items.
- HEIC originals from the 2026-08-23 ingest are archived at
  `202:~/altacloset/archive/2026-08-23-wife-photos-158/`.

## Data hygiene — dedup + orientation

- **Duplicate gate (ingest, BEFORE items enter the app):** `ingest_batch.py`
  orients the photo first, computes its dHash + color class, then:
  - a **clear duplicate** (dHash distance ≤ `SIMILAR_THRESHOLD`=8, any category,
    color-matched) is **skipped** — it never creates a garment;
  - a **debatable match** (≤ `DEBATE_THRESHOLD`=20, cross-category) is **created
    but flagged** as a "possible duplicate".
- **Possible-duplicate notes:** the wardrobe grid/detail show "⚠ similar to X —
  possible duplicate" via `media.garment_dict` (now category-agnostic + debate
  zone), so a plaid shirt imported once as `top` and once as `outerwear` is
  caught even when the ingest categories differ.
- **Orientation:** `aifill.ai_orientation` reads the tag at 0/90/180/270 and
  returns the fix rotation — upside-down gets 180, sideways gets 90/270.
  `normalize_orientation` applies it; only a deliberate 90/270 may leave a
  horizontal frame (0/180 always stay portrait).
- **Re-process existing photos:** `scripts/fix_orientation.py` re-checks a user's
  existing garments (optional `--category`) and re-saves + re-tags any that are
  rotated. Run inside the webapp container on 187:
  `docker exec -i altacloset-webapp python - < scripts/fix_orientation.py --email <user> [--category bottom] [--dry-run]`
- **Cleanup 2026-08-23:** removed 3 clear duplicates from user 7 (Maroon blazer,
  Navy blazer, Plaid skirt — keeping the first-created), cleaned 3 orphaned
  embeddings. DB backed up at `data/db/altacloset.db.pre-fix-20260823-214847`.

## Model paths

- Vision: `/mnt/models/altacloset/vision/Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf` + `mmproj-F16.gguf`
- FashionCLIP HF cache: `/mnt/models/altacloset/rec/hf`
- ALS factors (webapp reads): `data/rec/als.npz` on 187
