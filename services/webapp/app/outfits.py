"""Per-user saved outfits.

An outfit is just an ordered list of garment ids (top → bottom, or a single
dress) that the user saved from the Try-on look builder. Stored as JSON in the
`outfits` table; garment details are resolved by the caller against the wardrobe
so deleted garments are dropped gracefully.
"""
from __future__ import annotations

import json
from typing import Any

from . import db


class OutfitStore:
    def __init__(self) -> None:
        self._conn = db.init()
        self._lock = db.lock()

    def list(self, user_id: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM outfits WHERE user_id=? ORDER BY id DESC", (user_id,)
            ).fetchall()
        return [self._row(r) for r in rows]

    def create(
        self, user_id: int, name: str, garment_ids: list[int], result_url: str = ""
    ) -> dict[str, Any]:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO outfits (user_id, name, garment_ids, result_url) VALUES (?,?,?,?)",
                (user_id, name, json.dumps([int(x) for x in garment_ids]), result_url or ""),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM outfits WHERE id=?", (cur.lastrowid,)
            ).fetchone()
        return self._row(row)

    def delete(self, user_id: int, outfit_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM outfits WHERE user_id=? AND id=?", (user_id, outfit_id)
            )
            self._conn.commit()
            return cur.rowcount > 0

    @staticmethod
    def _row(r: Any) -> dict[str, Any]:
        try:
            ids = json.loads(r["garment_ids"] or "[]")
        except json.JSONDecodeError:
            ids = []
        return {
            "id": r["id"],
            "user_id": r["user_id"],
            "name": r["name"],
            "garment_ids": [int(x) for x in ids],
            "result_url": r["result_url"] if "result_url" in r.keys() else "",
            "created_at": r["created_at"],
        }
