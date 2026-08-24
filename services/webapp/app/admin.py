"""Dev-only admin + test-sandbox copy.

Gated by DEV_ADMIN_ENABLED (see config). Two fixed dev logins (username, not
email) — `admin` and `test` — both with the same dev password by default.

  admin  — the master account. From the Account page it can list real accounts
           and "act as" any of them: the admin's session resolves to that user
           row, so they see AND change everything exactly as that user would
           (switch user / update any info). Only runs on the dev instance.
  test   — a sandbox. Its account data is a COPY of a real user's data (a
           snapshot taken at copy time). Test can run the app freely and make
           changes, but everything lands on the copy — no live adjustments.

How the `test` copy stays isolated:
  Every row is copied into the test user's id and every image file is copied
  into that user's directories, so nothing is shared with the real account.
  Refreshing the copy clears the old test data first, then copies the user's
  CURRENT data ("copy it now").
"""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Any

from . import auth, db
from .config import settings

WARDROBE_DIR = Path(settings.data_dir) / "wardrobe"
UPLOAD_DIR = Path(settings.data_dir) / "uploads"


class AdminError(Exception):
    """User-facing dev-admin failure (missing user, no test account...)."""


def dev_admin_enabled() -> bool:
    return settings.dev_admin_enabled


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #
def list_users() -> list[dict[str, Any]]:
    """Real accounts (dev `admin`/`test` excluded), with per-account counts."""
    conn = db.init()
    with db.lock():
        rows = conn.execute(
            """SELECT u.id, u.email, u.created_at,
                      (SELECT COUNT(*) FROM garments g WHERE g.user_id=u.id)    AS garment_count,
                      (SELECT COUNT(*) FROM outfits o  WHERE o.user_id=u.id)    AS outfit_count,
                      (SELECT COUNT(*) FROM photos p   WHERE p.user_id=u.id)    AS photo_count
               FROM users u
               WHERE COALESCE(u.role, 'user') = 'user'
               ORDER BY u.id"""
        ).fetchall()
    return [dict(r) for r in rows]


def _first_real_user() -> dict[str, Any] | None:
    conn = db.init()
    with db.lock():
        row = conn.execute(
            "SELECT id, email FROM users WHERE COALESCE(role,'user')='user' ORDER BY id LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------- #
# Copy machinery (copy a real user's data into a target user id)
# --------------------------------------------------------------------------- #
def _next_id(conn, table: str) -> int:
    return conn.execute(f"SELECT COALESCE(MAX(id),0)+1 FROM {table}").fetchone()[0]


def _insert_copy(conn, table: str, row: Any, overrides: dict[str, Any]) -> None:
    """Insert a copied row into `table`, applying column overrides. The row is
    taken as-is from a SELECT * (sqlite3.Row) so column drift is tolerated."""
    d = dict(row)
    for k, v in overrides.items():
        d[k] = v
    cols = list(d.keys())
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
    conn.execute(sql, [d.get(c) for c in cols])


def _copy_garment_files(orig_id: int, target_id: int, gmap: dict[int, int]) -> None:
    src_dir = WARDROBE_DIR / str(orig_id)
    dst_dir = WARDROBE_DIR / str(target_id)
    if not src_dir.is_dir():
        return
    dst_dir.mkdir(parents=True, exist_ok=True)
    for old, new in gmap.items():
        for src in sorted(src_dir.glob(f"{old}.*")):
            if src.is_file():
                shutil.copy2(src, dst_dir / f"{new}{src.suffix.lower()}")


def _copy_garments(conn, orig_id: int, target_id: int) -> dict[int, int]:
    """Copy garments into the target user, remapping ids so the copy never
    collides with any other user. Returns the old→new id map."""
    gmap: dict[int, int] = {}
    for row in conn.execute("SELECT * FROM garments WHERE user_id=?", (orig_id,)).fetchall():
        old = row["id"]
        new = _next_id(conn, "garments")
        gmap[old] = new
        overrides: dict[str, Any] = {"id": new, "user_id": target_id}
        if "image_path" in row.keys() and row["image_path"]:
            overrides["image_path"] = f"{new}.{Path(row['image_path']).suffix.lstrip('.')}"
        _insert_copy(conn, "garments", row, overrides)
    _copy_garment_files(orig_id, target_id, gmap)
    return gmap


def _copy_photos(conn, orig_id: int, target_id: int) -> dict[int, int]:
    pmap: dict[int, int] = {}
    src_dir = UPLOAD_DIR / str(orig_id) / "photos"
    dst_dir = UPLOAD_DIR / str(target_id) / "photos"
    for row in conn.execute("SELECT * FROM photos WHERE user_id=?", (orig_id,)).fetchall():
        old = row["id"]
        new = _next_id(conn, "photos")
        pmap[old] = new
        ext = Path(row["filename"] or f"{old}.jpg").suffix or ".jpg"
        src = src_dir / row["filename"]
        if src.is_file():
            dst_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst_dir / f"{new}{ext}")
        _insert_copy(
            conn, "photos", row,
            {"id": new, "user_id": target_id, "filename": f"{new}{ext}"},
        )
    return pmap


def _copy_outfits(conn, orig_id: int, target_id: int, gmap: dict[int, int], pmap: dict[int, int]) -> dict[int, int]:
    omap: dict[int, int] = {}
    for row in conn.execute("SELECT * FROM outfits WHERE user_id=?", (orig_id,)).fetchall():
        old = row["id"]
        new = _next_id(conn, "outfits")
        omap[old] = new
        d = dict(row)
        try:
            gids = json.loads(row["garment_ids"] or "[]")
        except (json.JSONDecodeError, TypeError):
            gids = []
        d["garment_ids"] = json.dumps([gmap.get(int(g), int(g)) for g in gids])
        if row["person_photo_id"]:
            d["person_photo_id"] = pmap.get(row["person_photo_id"], row["person_photo_id"])
        _insert_copy(conn, "outfits", d, {"id": new, "user_id": target_id})
    return omap


def _copy_clips(conn, orig_id: int, target_id: int, omap: dict[int, int]) -> None:
    for row in conn.execute("SELECT * FROM clips WHERE user_id=?", (orig_id,)).fetchall():
        d = dict(row)
        if row["outfit_id"]:
            d["outfit_id"] = omap.get(row["outfit_id"], row["outfit_id"])
        _insert_copy(conn, "clips", d, {"id": _next_id(conn, "clips"), "user_id": target_id})


def _copy_chat(conn, orig_id: int, target_id: int) -> None:
    # chat_sessions.id is the PRIMARY KEY — a copy MUST get a fresh UUID, not
    # the original's (the original row still exists, so reusing its id would
    # hit UNIQUE constraint failed: chat_sessions.id).
    for row in conn.execute("SELECT * FROM chat_sessions WHERE user_id=?", (orig_id,)).fetchall():
        _insert_copy(conn, "chat_sessions", row, {"id": str(uuid.uuid4()), "user_id": target_id})


def _copy_interactions(conn, orig_id: int, target_id: int, gmap: dict[int, int]) -> None:
    for row in conn.execute("SELECT * FROM interactions WHERE user_id=?", (orig_id,)).fetchall():
        d = dict(row)
        d["garment_id"] = gmap.get(row["garment_id"], row["garment_id"])
        _insert_copy(conn, "interactions", d, {"id": _next_id(conn, "interactions"), "user_id": target_id})


def _copy_user_garment_state(conn, orig_id: int, target_id: int, gmap: dict[int, int]) -> None:
    for row in conn.execute("SELECT * FROM user_garment_state WHERE user_id=?", (orig_id,)).fetchall():
        d = dict(row)
        d["garment_id"] = gmap.get(row["garment_id"], row["garment_id"])
        _insert_copy(conn, "user_garment_state", d, {"user_id": target_id})


def _copy_group_members(conn, orig_id: int, target_id: int) -> None:
    # same family groups → shared items show exactly as they do for the real user
    for row in conn.execute("SELECT group_id FROM group_members WHERE user_id=?", (orig_id,)).fetchall():
        conn.execute(
            "INSERT OR IGNORE INTO group_members (group_id, user_id) VALUES (?,?)",
            (row["group_id"], target_id),
        )


def _copy_out_dir(orig_id: int, target_id: int) -> None:
    """Copy try-on render outputs (/api/uploads/... files) so saved outfit
    results + motion clips resolve under the target user's dir."""
    src = UPLOAD_DIR / str(orig_id) / "out"
    if src.is_dir():
        shutil.copytree(src, UPLOAD_DIR / str(target_id) / "out", dirs_exist_ok=True)


def _clear_user_data(conn, target_id: int) -> None:
    """Delete a user's rows + image files (used to refresh the test copy)."""
    for t in ("garments", "photos", "outfits", "clips", "chat_sessions", "interactions"):
        conn.execute(f"DELETE FROM {t} WHERE user_id=?", (target_id,))
    conn.execute("DELETE FROM user_garment_state WHERE user_id=?", (target_id,))
    conn.execute("DELETE FROM group_members WHERE user_id=?", (target_id,))
    shutil.rmtree(WARDROBE_DIR / str(target_id), ignore_errors=True)
    shutil.rmtree(UPLOAD_DIR / str(target_id), ignore_errors=True)


def copy_into_user(from_id: int, target_id: int) -> dict[str, Any]:
    """Copy a real user's data into `target_id` (replacing whatever it had).
    Every row is copied under new ids and every image is copied into the
    target's dirs — nothing is shared with the real account."""
    conn = db.init()
    orig = auth.get_user(from_id)
    if orig is None:
        raise AdminError("no such user")
    with db.lock():
        _clear_user_data(conn, target_id)
        gmap = _copy_garments(conn, from_id, target_id)
        pmap = _copy_photos(conn, from_id, target_id)
        omap = _copy_outfits(conn, from_id, target_id, gmap, pmap)
        _copy_clips(conn, from_id, target_id, omap)
        _copy_chat(conn, from_id, target_id)
        _copy_interactions(conn, from_id, target_id, gmap)
        _copy_user_garment_state(conn, from_id, target_id, gmap)
        _copy_group_members(conn, from_id, target_id)
        conn.commit()
    _copy_out_dir(from_id, target_id)
    return {"from_email": orig["email"], "target_id": target_id}


def copy_into_test(from_id: int) -> dict[str, Any]:
    """Refresh the `test` sandbox with a fresh copy of a real user's data."""
    test = auth.get_dev_user("test")
    if test is None:
        raise AdminError("test account not found (DEV_ADMIN_ENABLED=1 creates it)")
    return copy_into_user(from_id, test["id"])


def ensure_test_copy() -> dict[str, Any] | None:
    """If the test sandbox has no data yet, seed it from the first real user
    ("copy it now"). Returns info when it copied, else None."""
    test = auth.get_dev_user("test")
    if test is None:
        return None
    conn = db.init()
    with db.lock():
        has = conn.execute(
            "SELECT 1 FROM garments WHERE user_id=? LIMIT 1", (test["id"],)
        ).fetchone()
    if has:
        return None
    first = _first_real_user()
    if first is None:
        return None
    return copy_into_test(first["id"])


def test_copy_info() -> dict[str, Any]:
    """Info about the test sandbox for the admin console."""
    test = auth.get_dev_user("test")
    if test is None:
        return {"exists": False}
    conn = db.init()
    with db.lock():
        counts = conn.execute(
            """SELECT
                 (SELECT COUNT(*) FROM garments g WHERE g.user_id=?) AS garments,
                 (SELECT COUNT(*) FROM outfits o  WHERE o.user_id=?) AS outfits,
                 (SELECT COUNT(*) FROM photos p   WHERE p.user_id=?) AS photos""",
            (test["id"], test["id"], test["id"]),
        ).fetchone()
    return {"exists": True, "email": test["email"], "id": test["id"],
            "counts": dict(counts), "source": test_copy_source()}


# --------------------------------------------------------------------------- #
# Frozen test-snapshots (switch the `test` sandbox between non-live copies)     #
#                                                                            #
# A snapshot = a hidden role='snapshot' user holding a frozen copy of a real  #
# user's data (made with the same copy_into_user machinery — separate rows +  #
# copied image files, never shared with live). Switching the `test` sandbox   #
# copies FROM the hidden snapshot user INTO the test user, so the test user   #
# sees a stable, non-live copy and its own changes never touch live data.     #
# --------------------------------------------------------------------------- #

def _create_snapshot_user(real_uid: int) -> int:
    """Create a hidden role='snapshot' user nobody can log into; returns id."""
    conn = db.init()
    email = f"snapshot-{real_uid}@dev.local"
    with db.lock():
        row = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if row:
            return row["id"]
        salt, digest = auth.make_unusable_credentials()
        cur = conn.execute(
            "INSERT INTO users (email, password_salt, password_hash, role) VALUES (?,?,?,?)",
            (email, salt, digest, "snapshot"),
        )
        conn.commit()
        return cur.lastrowid


def snapshot_user(real_uid: int) -> dict[str, Any]:
    """Frozen snapshot of a real user (stored as a hidden role='snapshot' copy).
    Re-snapshotting refreshes the frozen copy from the user's current data."""
    real = auth.get_user(real_uid)
    if real is None or (real.get("role") or "user") != "user":
        raise AdminError("no such real user")
    conn = db.init()
    with db.lock():
        row = conn.execute(
            "SELECT snap_user_id FROM test_snapshots WHERE user_id=?", (real_uid,)
        ).fetchone()
        snap_id = row["snap_user_id"] if row else None
    if snap_id is None:
        snap_id = _create_snapshot_user(real_uid)
    copy_into_user(real_uid, snap_id)  # (re)fill the frozen copy
    with db.lock():
        conn.execute(
            """INSERT INTO test_snapshots (user_id, email, snap_user_id, created_at)
               VALUES (?,?,?,datetime('now'))
               ON CONFLICT(user_id) DO UPDATE SET
                 email=excluded.email, snap_user_id=excluded.snap_user_id,
                 created_at=datetime('now')""",
            (real_uid, real["email"], snap_id),
        )
        conn.commit()
    return {"user_id": real_uid, "email": real["email"], "snap_user_id": snap_id}


def snapshot_all_users() -> dict[str, Any]:
    """Snapshot every real user so the test sandbox can switch between them."""
    made = [snapshot_user(u["id"]) for u in list_users()]
    return {"snapshotted": [m["user_id"] for m in made], "count": len(made)}


def list_snapshots() -> list[dict[str, Any]]:
    """Catalog of frozen snapshots + which one the test sandbox is on."""
    test = auth.get_dev_user("test")
    active = test.get("test_source") if test else None
    conn = db.init()
    with db.lock():
        rows = conn.execute("SELECT * FROM test_snapshots ORDER BY email").fetchall()
    return [
        {"user_id": r["user_id"], "email": r["email"],
         "snap_user_id": r["snap_user_id"], "created_at": r["created_at"],
         "active": (r["user_id"] == active)}
        for r in rows
    ]


def switch_test_to(real_uid: int) -> dict[str, Any]:
    """Point the `test` sandbox at a frozen snapshot of a real user (non-live).
    The test user's data is replaced from the snapshot; live data is untouched."""
    test = auth.get_dev_user("test")
    if test is None:
        raise AdminError("test account not found (DEV_ADMIN_ENABLED=1 creates it)")
    conn = db.init()
    with db.lock():
        row = conn.execute(
            "SELECT snap_user_id, email FROM test_snapshots WHERE user_id=?", (real_uid,)
        ).fetchone()
    if row is None:
        raise AdminError("no snapshot for that user — run 'Snapshot all users' as admin first")
    copy_into_user(row["snap_user_id"], test["id"])
    with db.lock():
        conn.execute("UPDATE users SET test_source=? WHERE id=?", (real_uid, test["id"]))
        conn.commit()
    return {"ok": True, "copied_from": row["email"], "test_source": real_uid}


def test_copy_source() -> dict[str, Any] | None:
    """Real user the test sandbox currently mirrors (for /me + the console)."""
    test = auth.get_dev_user("test")
    if test is None or not test.get("test_source"):
        return None
    real = auth.get_user(test["test_source"])
    if real is None:
        return None
    return {"id": real["id"], "email": real["email"]}
