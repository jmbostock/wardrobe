"""Interaction log — the learning fuel for the recommendation engine.

Every meaningful user↔garment event is recorded with a timestamp + a confidence
weight so the engine can learn from recommendations over time (per-user style
centroid + ALS collaborative filter in a later phase). Kinds:

  shown       a recommended outfit was displayed (one row per garment in it)
  tried_on    a /api/tryon* render was requested
  saved       the garment was saved into an outfit
  rated_up    the garment/outfit was rated 7–10
  rated_down  the garment/outfit was rated 1–3
  liked       explicit thumbs-up on a suggestion card
  disliked    explicit thumbs-down on a suggestion card
  worn        the garment was actually worn (future "did you wear it?" feedback)

Weights map each event to a confidence value for the ALS matrix later.
"""
from __future__ import annotations

import json

from . import db

KINDS = ("shown", "tried_on", "saved", "rated_up", "rated_down", "liked", "disliked", "worn")

WEIGHTS: dict[str, float] = {
    "shown": 0.5,
    "tried_on": 2.0,
    "saved": 3.0,
    "rated_up": 4.0,
    "rated_down": -2.0,
    "liked": 2.0,
    "disliked": -3.0,
    "worn": 4.0,
}


def log(user_id: int, garment_id: int, kind: str, context: dict | None = None) -> None:
    """Record one interaction. Unknown kinds are ignored (defensive)."""
    if kind not in WEIGHTS:
        return
    conn = db.init()
    with db.lock():
        conn.execute(
            "INSERT INTO interactions (user_id, garment_id, kind, weight, context) "
            "VALUES (?,?,?,?,?)",
            (user_id, garment_id, kind, WEIGHTS[kind], json.dumps(context or {})),
        )
        conn.commit()


def log_outfit_shown(user_id: int, outfit: dict, context: dict | None = None) -> None:
    """Log `shown` for every garment in a recommended outfit dict."""
    ids: list[int] = []
    for slot in ("top", "bottom", "outerwear", "footwear"):
        g = outfit.get(slot)
        if g and g.get("id"):
            ids.append(g["id"])
    for a in outfit.get("accessories") or []:
        if a and a.get("id"):
            ids.append(a["id"])
    for gid in ids:
        log(user_id, gid, "shown", context)


def log_many(user_id: int, garment_ids: list[int], kind: str, context: dict | None = None) -> None:
    for gid in garment_ids:
        if gid:
            log(user_id, gid, kind, context)


def recent(user_id: int, limit: int = 500) -> list[dict]:
    """Most recent interactions for a user (newest first) — for eval/analytics."""
    conn = db.init()
    with db.lock():
        rows = conn.execute(
            "SELECT garment_id, kind, weight, context, created_at FROM interactions "
            "WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]
