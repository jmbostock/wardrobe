#!/usr/bin/env python3
"""Rec-engine batch builder — FashionCLIP embeddings + ALS model (Layer 2/3).

Run in the ML venv (requirements-ml.txt), after adding a batch of clothes or on
a schedule:

    ~/rec-ml/bin/python scripts/rec_build.py               # embed + train
    ~/rec-ml/bin/python scripts/rec_build.py --embed-only  # FashionCLIP only
    ~/rec-ml/bin/python scripts/rec_build.py --als-only    # ALS only

Tip: set OPENBLAS_NUM_THREADS=1 in the venv to silence the implicit/OpenBLAS
threadpool warning (and for a tiny speedup at our scale).

Writes:
  - `garment_embeddings` table (fashion-clip-v2, 512-d) for every garment image
  - `data/rec/als.npz` — implicit ALS user/item factors

The webapp reads both with numpy only (no torch at request time). The embed step
downloads FashionCLIP weights on first run; embeddings are idempotent (garments
that already have a vector are skipped). ALS only trains once there are enough
real interactions (MIN_TRAIN_INTERACTIONS) to be meaningful; it can also be
re-run to refresh as the interaction log grows.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import scipy.sparse as sp

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "webapp"))

from app import db, embeddings  # noqa: E402
from app.config import settings  # noqa: E402
from app.media import garment_image_path  # noqa: E402

MODEL_NAME = "fashion-clip-v2"
ALS_DIM = 64
MIN_TRAIN_INTERACTIONS = 20  # don't bother training a model on a trickle of data


def _iter_unembedded() -> list[tuple[int, int, Path]]:
    conn = db.init()
    with db.lock():
        rows = conn.execute("SELECT id, user_id FROM garments").fetchall()
    todo: list[tuple[int, int, Path]] = []
    for r in rows:
        gid, uid = r["id"], r["user_id"]
        if embeddings.get_vector(gid) is not None:
            continue
        p = garment_image_path(uid, gid)
        if p is not None:
            todo.append((gid, uid, p))
    return todo


def embed_all() -> tuple[int, int]:
    """FashionCLIP-embed every garment image that lacks a vector. (done, skipped)."""
    from PIL import Image

    from fashion_clip.fashion_clip import FashionCLIP

    clip = FashionCLIP(MODEL_NAME)  # downloads weights on first run
    todo = _iter_unembedded()
    print(f"[embed] {len(todo)} garments need embeddings")
    done = skipped = 0
    for gid, _uid, path in todo:
        try:
            img = Image.open(path).convert("RGB")
            vec = clip.encode_images([img], batch_size=1)[0]
            embeddings.save_embedding(gid, np.asarray(vec), model=MODEL_NAME)
            done += 1
        except Exception as ex:  # noqa: BLE001 — keep going, report the miss
            skipped += 1
            print(f"  ! garment {gid} failed: {ex}")
    print(f"[embed] done={done} skipped={skipped} total={embeddings.count_embeddings()}")
    return done, skipped


def _interaction_rows() -> list[tuple[int, int, float]]:
    conn = db.init()
    with db.lock():
        rows = conn.execute(
            "SELECT user_id, garment_id, weight FROM interactions WHERE kind != 'shown'"
        ).fetchall()
    return [(r[0], r[1], float(r[2] or 1.0)) for r in rows]


def build_matrix(rows: list[tuple[int, int, float]]) -> tuple[sp.csr_matrix, list[int], list[int]]:
    """Build the user×item confidence CSR matrix + the id→index mappings.
    Exposed for tests (scipy-only; implicit is not needed here)."""
    users = sorted({r[0] for r in rows})
    items = sorted({r[1] for r in rows})
    uidx = {u: i for i, u in enumerate(users)}
    iidx = {item: idx for idx, item in enumerate(items)}
    data = [r[2] for r in rows]
    row = [uidx[r[0]] for r in rows]
    col = [iidx[r[1]] for r in rows]
    matrix = sp.coo_matrix((data, (row, col)), shape=(len(users), len(items))).tocsr()
    return matrix, users, items


def save_model(out_dir: Path, users: list[int], items: list[int],
               user_factors, item_factors) -> Path:
    """Write the ALS factors npz in the exact shape app.learner.load_model reads."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "als.npz"
    np.savez(
        out,
        user_ids=np.asarray(users, dtype=np.int64),
        item_ids=np.asarray(items, dtype=np.int64),
        user_factors=user_factors,
        item_factors=item_factors,
    )
    return out


def train_als() -> bool:
    """Train implicit ALS on the interaction log and save factors to als.npz.
    Returns True if a model was written, False if there wasn't enough data."""
    from implicit.als import AlternatingLeastSquares

    rows = _interaction_rows()
    if len(rows) < MIN_TRAIN_INTERACTIONS:
        print(
            f"[als] only {len(rows)} non-'shown' interactions "
            f"(< {MIN_TRAIN_INTERACTIONS}) — skipping; no model written"
        )
        return False
    matrix, users, items = build_matrix(rows)

    model = AlternatingLeastSquares(
        factors=ALS_DIM, regularization=0.05, alpha=40.0,
        iterations=30, random_state=7, use_gpu=False,
    )
    print(
        f"[als] training on {len(rows)} interactions "
        f"({len(users)} users × {len(items)} items)"
    )
    t0 = time.time()
    model.fit(matrix, show_progress=True)
    print(f"[als] trained in {time.time() - t0:.1f}s")

    out = save_model(
        Path(settings.data_dir) / "rec", users, items,
        model.user_factors, model.item_factors,
    )
    print(f"[als] saved {out}")
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description="Build rec-engine embeddings + ALS model")
    ap.add_argument("--embed-only", action="store_true", help="only run FashionCLIP embedding")
    ap.add_argument("--als-only", action="store_true", help="only train the ALS model")
    args = ap.parse_args()
    if not args.embed_only:
        train_als()
    if not args.als_only:
        embed_all()


if __name__ == "__main__":
    main()
