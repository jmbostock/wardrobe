"""Clip generation jobs — SVD motion renders of try-on results.

Each clip row tracks a ComfyUI prompt. The webapp submits the SVD job and
returns immediately with the clip id; the frontend polls GET /api/clips/{id}.
Since ComfyUI queues prompts server-side, several clips (or a clip + a try-on)
can be submitted back-to-back and they'll run one after another — the webapp
never blocks on a long SVD render.
"""
from __future__ import annotations

from typing import Any

from . import db


class ClipStore:
    def __init__(self) -> None:
        self._conn = db.init()
        self._lock = db.lock()

    def create(self, user_id: int, prompt_id: str, outfit_id: int = 0) -> dict[str, Any]:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO clips (user_id, prompt_id, status, outfit_id) VALUES (?,?,?,?)",
                (user_id, prompt_id, "queued", outfit_id or 0),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM clips WHERE id=?", (cur.lastrowid,)
            ).fetchone()
        return dict(row)

    def get(self, user_id: int, clip_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM clips WHERE id=? AND user_id=?", (clip_id, user_id)
            ).fetchone()
        return dict(row) if row else None

    def update(
        self, user_id: int, clip_id: int, *, status: str | None = None,
        result_url: str | None = None, error: str | None = None,
    ) -> bool:
        fields = {}
        if status is not None:
            fields["status"] = status
        if result_url is not None:
            fields["result_url"] = result_url
        if error is not None:
            fields["error"] = error
        if not fields:
            return False
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE clips SET {sets} WHERE id=? AND user_id=?",
                (*fields.values(), clip_id, user_id),
            )
            self._conn.commit()
            return cur.rowcount > 0
