"""ALS collaborative-filter model loading + scoring (rec-engine Layer 3).

The model itself (user/item factors) is trained by `scripts/rec_build.py` using
the `implicit` library and saved as a numpy npz at `data/rec/als.npz`. At request
time this module is **numpy-only**: it loads the factors and scores a (user,
garment) pair as a dot product. The ALS term is only added once the user has
enough real interactions (MIN_INTERACTIONS) — otherwise the recommender stays
rules + style, which is more stable for a cold user.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from . import db
from .config import settings

MODEL_PATH = Path(settings.data_dir) / "rec" / "als.npz"
MIN_INTERACTIONS = 10


def load_model() -> dict | None:
    """Load the ALS factors npz, or None if it doesn't exist / is corrupt."""
    if not MODEL_PATH.is_file():
        return None
    try:
        data = np.load(MODEL_PATH, allow_pickle=False)
        return {
            "user_ids": data["user_ids"],
            "item_ids": data["item_ids"],
            "user_factors": data["user_factors"],
            "item_factors": data["item_factors"],
        }
    except Exception:  # noqa: BLE001 — corrupt/partial model → treat as absent
        return None


def _index(ids: np.ndarray, val: int) -> int | None:
    hit = np.where(ids == val)[0]
    return int(hit[0]) if len(hit) else None


def als_score(user_id: int, garment_id: int, model: dict) -> float | None:
    """Dot-product affinity of (user, garment) in the factor space, or None if
    either side isn't in the trained model."""
    ui = _index(model["user_ids"], user_id)
    ii = _index(model["item_ids"], garment_id)
    if ui is None or ii is None:
        return None
    return float(np.dot(model["user_factors"][ui], model["item_factors"][ii]))


def interaction_count(user_id: int) -> int:
    conn = db.init()
    with db.lock():
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM interactions WHERE user_id=?", (user_id,)
        ).fetchone()
    return row["n"] if row else 0
