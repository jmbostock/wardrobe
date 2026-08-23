"""Garment image embeddings + per-user style vector (rec-engine Layer 2).

FashionCLIP vectors are written by `scripts/rec_build.py` (batch, ML venv) into
the `garment_embeddings` table. At request time this module is **numpy-only**:
it loads vectors and computes the user's style centroid — a weighted, time-decayed
mean of the embeddings of garments the user actually engaged with (worn / saved /
tried on / rated / liked / disliked). `user_style_vector()` is the "gets to know
your taste" signal the recommender adds as a bonus term.

Requires ≥ MIN_STYLE_GARMENTS distinct engaged garments; older interactions decay
with a half-life (default 90 days) so taste drifts naturally.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from . import db

MODEL = "fashion-clip-v2"
DIM = 512
MIN_STYLE_GARMENTS = 3          # fewer engaged garments → no style vector yet
STYLE_HALF_LIFE_DAYS = 90.0     # taste-decay half-life

# kinds that DON'T count toward the style centroid ("shown" is just display noise)
_EXCLUDED_KINDS = {"shown"}


def _pack(vector: np.ndarray) -> bytes:
    return np.asarray(vector, dtype=np.float32).tobytes()


def _unpack(data: bytes | None, dim: int) -> np.ndarray | None:
    if not data:
        return None
    try:
        return np.frombuffer(data, dtype=np.float32).copy()
    except Exception:  # noqa: BLE001 — corrupt blob → treat as missing
        return None


# --------------------------------------------------------------------------- #
# persistence                                                                 #
# --------------------------------------------------------------------------- #

def save_embedding(garment_id: int, vector: np.ndarray, model: str = MODEL) -> None:
    conn = db.init()
    vec = np.asarray(vector, dtype=np.float32)
    with db.lock():
        conn.execute(
            """INSERT INTO garment_embeddings (garment_id, model, dim, vector, updated_at)
               VALUES (?,?,?,?,datetime('now'))
               ON CONFLICT(garment_id) DO UPDATE SET
                 model=excluded.model, dim=excluded.dim, vector=excluded.vector,
                 updated_at=datetime('now')""",
            (garment_id, model, int(vec.shape[0]), _pack(vec)),
        )
        conn.commit()


def get_vector(garment_id: int) -> np.ndarray | None:
    conn = db.init()
    with db.lock():
        row = conn.execute(
            "SELECT vector, dim FROM garment_embeddings WHERE garment_id=?", (garment_id,)
        ).fetchone()
    return _unpack(row["vector"], row["dim"]) if row else None


def all_vectors() -> dict[int, np.ndarray]:
    conn = db.init()
    with db.lock():
        rows = conn.execute(
            "SELECT garment_id, vector, dim FROM garment_embeddings"
        ).fetchall()
    return {r["garment_id"]: _unpack(r["vector"], r["dim"]) for r in rows}


def count_embeddings() -> int:
    conn = db.init()
    with db.lock():
        row = conn.execute("SELECT COUNT(*) AS n FROM garment_embeddings").fetchone()
    return row["n"] if row else 0


# --------------------------------------------------------------------------- #
# math                                                                        #
# --------------------------------------------------------------------------- #

def cosine(a: np.ndarray, b: np.ndarray) -> float:
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def user_style_vector(
    user_id: int,
    half_life_days: float = STYLE_HALF_LIFE_DAYS,
) -> np.ndarray | None:
    """Weighted, time-decayed mean of the user's engaged-garment embeddings.

    Returns a unit vector, or None when there aren't enough (or any) engaged
    garments with embeddings yet. Only strong signals count (worn/saved/tried/
    rated/liked/disliked); mere 'shown' impressions are excluded.
    """
    conn = db.init()
    with db.lock():
        rows = conn.execute(
            """SELECT i.garment_id, i.kind, i.weight, i.created_at,
                      e.vector AS vec, e.dim AS dim
               FROM interactions i
               JOIN garment_embeddings e ON e.garment_id = i.garment_id
               WHERE i.user_id = ? ORDER BY i.id DESC""",
            (user_id,),
        ).fetchall()
    engaged = [r for r in rows if r["kind"] not in _EXCLUDED_KINDS]
    if len(engaged) < MIN_STYLE_GARMENTS:
        return None

    now = datetime.now(timezone.utc)
    total: np.ndarray | None = None
    denom = 0.0
    for r in engaged:
        vec = _unpack(r["vec"], r["dim"])
        if vec is None:
            continue
        w = float(r["weight"] or 0.5)
        if w <= 0:
            continue
        if total is None:
            total = np.zeros_like(np.asarray(vec, dtype=float))
        try:
            ts = datetime.fromisoformat(r["created_at"])
            days = max(0.0, (now - ts).total_seconds() / 86400.0)
            w *= 0.5 ** (days / half_life_days)
        except (TypeError, ValueError):
            pass
        total += w * np.asarray(vec, dtype=float)
        denom += w
    if total is None or denom <= 0.0:
        return None
    v = total / denom
    n = float(np.linalg.norm(v))
    return v / n if n > 0.0 else None
