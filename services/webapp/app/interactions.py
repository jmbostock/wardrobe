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
from datetime import datetime, timezone

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

# --- feedback half-lives (days) ----------------------------------------------
# A "not feeling it right now" thumbs-down should DECAY quickly so a good item
# recovers; durable signals (likes/saves/wears/ratings) stick around far longer.
NEGATIVE_HALF_LIFE_DAYS = 14.0
POSITIVE_HALF_LIFE_DAYS = 365.0
# Feedback given for a different activity (e.g. a dislike for "office") counts
# less when scoring a different occasion, so it never buries the item there.
CROSS_ACTIVITY_DAMPEN = 0.4


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


def affinity_map(user_id: int, activity: str | None = None) -> dict[int, float]:
    """Per-garment feedback affinity for a user — the online learning signal.

    'shown' impressions are excluded (display noise). Each event is:
      - time-decayed (negative feedback recovers fast; positive feedback sticks)
      - context-weighted (feedback given for a DIFFERENT activity is dampened)
    so a single "not feeling it right now" thumbs-down fades and never permanently
    buries a good item, and a dislike for one occasion doesn't kill it for others.
    """
    conn = db.init()
    now = datetime.now(timezone.utc)
    with db.lock():
        rows = conn.execute(
            "SELECT garment_id, weight, context, created_at FROM interactions "
            "WHERE user_id=? AND kind != 'shown' ORDER BY id DESC",
            (user_id,),
        ).fetchall()

    ret: dict[int, float] = {}
    for r in rows:
        w = float(r["weight"] or 0.0)
        if not w:
            continue
        # time decay — a "just not feeling it today" fades; durable likes stay
        try:
            ts = datetime.fromisoformat(r["created_at"])
            days = max(0.0, (now - ts).total_seconds() / 86400.0)
        except (TypeError, ValueError):
            days = 0.0
        half_life = NEGATIVE_HALF_LIFE_DAYS if w < 0 else POSITIVE_HALF_LIFE_DAYS
        w *= 0.5 ** (days / half_life)
        # context weighting — feedback for a different activity counts less
        if activity:
            try:
                ctx = json.loads(r["context"] or "{}")
            except (json.JSONDecodeError, TypeError):
                ctx = {}
            a = (ctx.get("activity") or "").strip().lower()
            if a and a != activity.strip().lower():
                w *= CROSS_ACTIVITY_DAMPEN
        ret[r["garment_id"]] = ret.get(r["garment_id"], 0.0) + w
    return ret
