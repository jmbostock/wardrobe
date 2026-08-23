"""Family sharing + per-person garment state (rec-engine v2).

Flat "Family" group model (user decision 2026-08-23): ONE global Family group
and every user is a member. A garment owner marks an item "shared to Family"
(`garments.share_group_id = Family`); every other member then sees it — the
visible set for a user is owned ∪ family-shared. `user_garment_state` keeps
per-person wear_count / last_worn / rating / fit_ok so shared clothes rotate per
person and a shared item someone marks "doesn't fit" is never suggested to them.
"""
from __future__ import annotations

from . import db

FAMILY_GROUP_NAME = "Family"


def ensure_family(user_id: int) -> int:
    """Create the global Family group if missing and add the user to it. Returns
    the Family group id. Called at user creation / login / first share."""
    conn = db.init()
    with db.lock():
        conn.execute("INSERT OR IGNORE INTO groups (name) VALUES (?)", (FAMILY_GROUP_NAME,))
        row = conn.execute(
            "SELECT id FROM groups WHERE name=?", (FAMILY_GROUP_NAME,)
        ).fetchone()
        gid = row["id"]
        conn.execute(
            "INSERT OR IGNORE INTO group_members (group_id, user_id) VALUES (?,?)",
            (gid, user_id),
        )
        conn.commit()
    return gid


def user_group_ids(user_id: int) -> list[int]:
    """Group ids the user belongs to (read-only; membership is ensured at login)."""
    conn = db.init()
    with db.lock():
        rows = conn.execute(
            "SELECT group_id FROM group_members WHERE user_id=?", (user_id,)
        ).fetchall()
    return [r["group_id"] for r in rows]


def set_shared(user_id: int, garment_id: int, share: bool) -> bool:
    """Owner-only: mark a garment shared to Family (or private again)."""
    conn = db.init()
    gid = ensure_family(user_id)
    with db.lock():
        cur = conn.execute(
            "UPDATE garments SET share_group_id=? WHERE id=? AND user_id=?",
            (gid if share else None, garment_id, user_id),
        )
        conn.commit()
    return cur.rowcount > 0


def set_fit(user_id: int, garment_id: int, fit_ok: bool | None) -> bool:
    """Viewer-scoped: record whether this garment fits the user."""
    conn = db.init()
    val = None if fit_ok is None else (1 if fit_ok else 0)
    with db.lock():
        cur = conn.execute(
            """INSERT INTO user_garment_state (user_id, garment_id, fit_ok)
               VALUES (?,?,?)
               ON CONFLICT(user_id, garment_id) DO UPDATE SET fit_ok=excluded.fit_ok""",
            (user_id, garment_id, val),
        )
        conn.commit()
    return cur.rowcount > 0


def state(user_id: int, garment_id: int) -> dict:
    """Per-person garment state (empty defaults when none recorded)."""
    conn = db.init()
    with db.lock():
        row = conn.execute(
            "SELECT wear_count, last_worn, rating, fit_ok FROM user_garment_state "
            "WHERE user_id=? AND garment_id=?",
            (user_id, garment_id),
        ).fetchone()
    if not row:
        return {"wear_count": 0, "last_worn": None, "rating": 0, "fit_ok": None}
    return dict(row)
