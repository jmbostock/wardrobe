"""Per-user person photos.

Stored at DATA_DIR/uploads/<user_id>/photos/<id>.<ext>. One photo per user is the
"default" (pre-selected for try-on). Everything is owner-scoped; callers must pass
the authenticated user_id.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from . import db
from .config import settings

PHOTOS_DIR = Path(settings.data_dir) / "uploads"


class PhotoError(Exception):
    pass


def _photo_dir(user_id: int) -> Path:
    p = PHOTOS_DIR / str(user_id) / "photos"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "filename": row["filename"],
        "is_default": bool(row["is_default"]),
        "created_at": row["created_at"],
        "url": f"/api/photos/{row['id']}/image",
    }


def upload(user_id: int, data: bytes, ext: str = ".jpg") -> dict[str, Any]:
    conn = db.init()
    if not data:
        raise PhotoError("empty image")
    safe_ext = "".join(c for c in ext.lower() if c.isalnum() or c == ".")[:8] or ".jpg"
    if safe_ext not in (".jpg", ".jpeg", ".png", ".webp"):
        safe_ext = ".jpg"
    photo_id = int(time.time() * 1000) % (10**9)  # near-unique id
    name = f"{photo_id}{safe_ext}"
    (_photo_dir(user_id) / name).write_bytes(data)
    with db.lock():
        # first photo becomes the default automatically
        is_first = conn.execute(
            "SELECT COUNT(*) FROM photos WHERE user_id=?", (user_id,)
        ).fetchone()[0] == 0
        is_default = 1 if is_first else 0
        conn.execute(
            "INSERT INTO photos (user_id, filename, is_default) VALUES (?,?,?)",
            (user_id, name, is_default),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM photos WHERE user_id=? AND filename=?",
            (user_id, name),
        ).fetchone()
    return _row_to_dict(row)


def list(user_id: int) -> list[dict[str, Any]]:
    conn = db.init()
    with db.lock():
        rows = conn.execute(
            "SELECT * FROM photos WHERE user_id=? ORDER BY is_default DESC, id DESC",
            (user_id,),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _get(user_id: int, photo_id: int) -> Any | None:
    conn = db.init()
    with db.lock():
        return conn.execute(
            "SELECT * FROM photos WHERE user_id=? AND id=?", (user_id, photo_id)
        ).fetchone()


def set_default(user_id: int, photo_id: int) -> None:
    conn = db.init()
    if _get(user_id, photo_id) is None:
        raise PhotoError("photo not found")
    with db.lock():
        conn.execute("UPDATE photos SET is_default=0 WHERE user_id=?", (user_id,))
        conn.execute(
            "UPDATE photos SET is_default=1 WHERE user_id=? AND id=?",
            (user_id, photo_id),
        )
        conn.commit()


def delete(user_id: int, photo_id: int) -> None:
    conn = db.init()
    row = _get(user_id, photo_id)
    if row is None:
        raise PhotoError("photo not found")
    path = _photo_dir(user_id) / row["filename"]
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    with db.lock():
        conn.execute("DELETE FROM photos WHERE user_id=? AND id=?", (user_id, photo_id))
        conn.commit()
        # if we removed the default, promote the newest remaining photo
        if row["is_default"]:
            next_row = conn.execute(
                "SELECT id FROM photos WHERE user_id=? ORDER BY id DESC LIMIT 1",
                (user_id,),
            ).fetchone()
            if next_row:
                conn.execute(
                    "UPDATE photos SET is_default=1 WHERE user_id=? AND id=?",
                    (user_id, next_row["id"]),
                )
                conn.commit()


def photo_bytes(user_id: int, photo_id: int) -> bytes:
    row = _get(user_id, photo_id)
    if row is None:
        raise PhotoError("photo not found")
    path = _photo_dir(user_id) / row["filename"]
    if not path.is_file():
        raise PhotoError("photo file missing")
    return path.read_bytes()


def photo_path(user_id: int, photo_id: int) -> Path:
    row = _get(user_id, photo_id)
    if row is None:
        raise PhotoError("photo not found")
    return _photo_dir(user_id) / row["filename"]
