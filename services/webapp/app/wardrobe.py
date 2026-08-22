"""Wardrobe store — per-user garments.

Every garment belongs to a user (`user_id`). Each new user gets a copy of the
seed wardrobe so the MVP is demoable for everyone. Garment images live at
data/wardrobe/<user_id>/<garment_id>.png
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

from . import db

# name, category, color_hex, color_tags, warmth, waterproof, formality, occasions, material, fit
SEED_GARMENTS = [
    # tops
    ("White oxford button-down", "top", "#f2f1ec", "white,light", 2, 0, "business", "office,event", "cotton", "regular"),
    ("Navy merino crewneck", "top", "#1f2a44", "navy,dark", 4, 0, "smart-casual", "office,date,casual", "wool", "regular"),
    ("Light gray crew tee", "top", "#c8ccd2", "gray,light", 1, 0, "casual", "casual,active", "cotton", "regular"),
    ("Forest green flannel", "top", "#2e4a3a", "green,dark", 3, 0, "smart-casual", "casual,date", "cotton", "regular"),
    ("Black turtleneck", "top", "#1a1a1a", "black,dark", 4, 0, "smart-casual", "office,date,event", "wool", "slim"),
    ("Charcoal hoodie", "top", "#3b3b3b", "gray,dark", 3, 0, "casual", "casual,active,home", "cotton fleece", "regular"),
    ("White short-sleeve polo", "top", "#f2f2f2", "white,light", 2, 0, "smart-casual", "office,date,active", "pique cotton", "regular"),
    ("Burgundy knit sweater", "top", "#6d2332", "burgundy,dark,red", 4, 0, "smart-casual", "date,event,casual", "wool", "regular"),
    # bottoms
    ("Dark slim chinos", "bottom", "#3a3f47", "gray,dark", 3, 0, "business", "office,date", "cotton", "slim"),
    ("Blue straight jeans", "bottom", "#3b5ba8", "blue", 3, 0, "casual", "casual,active,date", "denim", "regular"),
    ("Black tapered trousers", "bottom", "#1c1c1c", "black,dark", 2, 0, "formal", "office,event", "wool blend", "slim"),
    ("Khaki shorts", "bottom", "#c8b98a", "tan,khaki,light", 1, 0, "casual", "active,beach,casual", "cotton", "regular"),
    # dresses
    ("Navy sheath dress", "dress", "#22304f", "navy,dark", 3, 0, "formal", "event,office,date", "polyester", "slim"),
    ("Floral sundress", "dress", "#d9b3a0", "floral,pink,light", 2, 0, "smart-casual", "date,casual", "cotton", "regular"),
    # outerwear
    ("Packable puffer jacket", "outerwear", "#2f3a4d", "navy,dark", 5, 1, "smart-casual", "casual,date,active", "synthetic", "regular"),
    ("Lightweight rain shell", "outerwear", "#2c4f46", "teal,dark", 2, 1, "smart-casual", "casual,active,office", "nylon", "regular"),
    ("Denim jacket", "outerwear", "#4a6ea8", "blue", 3, 0, "casual", "casual,date", "denim", "regular"),
    ("Wool overcoat", "outerwear", "#262626", "black,dark", 4, 0, "formal", "office,event,date", "wool", "regular"),
    # footwear
    ("Low-top white sneakers", "footwear", "#e8e8e8", "white,light", 1, 0, "casual", "casual,active,date", "canvas", "regular"),
    ("Brown leather boots", "footwear", "#6b4a2f", "brown,dark", 3, 0, "smart-casual", "date,casual,active", "leather", "regular"),
    ("Black dress shoes", "footwear", "#1c1c1c", "black,dark", 1, 0, "formal", "office,event", "leather", "regular"),
    # accessories
    ("Wool beanie", "accessory", "#333f4d", "navy,dark", 3, 0, "casual", "casual,active", "wool", "regular"),
    ("Cotton scarf", "accessory", "#6d2332", "burgundy,red", 2, 0, "smart-casual", "casual,date", "cotton", "regular"),
    ("Wide-brim sun hat", "accessory", "#d9c9a3", "tan,light", 1, 0, "casual", "active,beach", "straw", "regular"),
    ("Leather belt", "accessory", "#5a3b22", "brown,dark", 0, 0, "all", "office,date,event", "leather", "regular"),
]


@dataclass
class Garment:
    id: int
    user_id: int
    name: str
    category: str
    color_hex: str
    color_tags: str
    warmth: int
    waterproof: int
    formality: str
    occasions: str
    material: str
    fit: str
    brand: str = ""        # e.g. "Old Navy" (auto-filled from link/AI, editable)
    sizes: str = ""        # comma list e.g. "S,M,L" or "2,4,6" (from tags when visible)
    phash: str = ""        # 64-bit dHash (16 hex) for near-duplicate detection
    last_worn: str | None = None
    wear_count: int = 0
    rating: int = 0
    owned: int = 1  # 1 = own it, 0 = to buy / wishlist
    created_at: str = ""
    image_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["occasions"] = [o.strip() for o in (self.occasions or "").split(",") if o.strip()]
        d["color_tags"] = [c.strip() for c in (self.color_tags or "").split(",") if c.strip()]
        return d


class Wardrobe:
    """Per-user garments store backed by the shared sqlite connection."""

    def __init__(self) -> None:
        self._conn = db.init()
        self._lock = db.lock()

    def seed_for_user(self, user_id: int) -> None:
        """No-op. The generic seed wardrobe was removed (2026-08-21): wardrobes
        start empty and users add their own clothes (upload / product link) in
        the Wardrobe tab. Kept as a method so callers don't break; does nothing.
        """
        return

    def remove_seed_garments(self, user_id: int) -> int:
        """Delete the generic seed garments for a user (keeps user-added ones).
        Seed rows are identified by their exact names from SEED_GARMENTS
        (row[0] = name) — user-added garments have arbitrary names, so they're
        never matched."""
        names = [row[0] for row in SEED_GARMENTS]
        placeholders = ",".join("?" * len(names))
        with self._lock:
            cur = self._conn.execute(
                f"DELETE FROM garments WHERE user_id=? AND name IN ({placeholders})",
                (user_id, *names),
            )
            self._conn.commit()
            return cur.rowcount

    def all(self, user_id: int, category: str | None = None) -> list[Garment]:
        with self._lock:
            if category:
                rows = self._conn.execute(
                    "SELECT * FROM garments WHERE user_id=? AND category=?",
                    (user_id, category),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM garments WHERE user_id=?", (user_id,)
                ).fetchall()
        return [self._row_to_garment(r) for r in rows]

    def get(self, user_id: int, garment_id: int) -> Garment | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM garments WHERE user_id=? AND id=?", (user_id, garment_id)
            ).fetchone()
        return self._row_to_garment(row) if row else None

    def mark_worn(self, user_id: int, garment_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE garments SET wear_count = wear_count + 1, last_worn = date('now') "
                "WHERE user_id=? AND id=?",
                (user_id, garment_id),
            )
            self._conn.commit()

    def create(
        self,
        user_id: int,
        name: str,
        category: str,
        color_hex: str = "",
        color_tags: str = "",
        warmth: int = 3,
        waterproof: int = 0,
        formality: str = "casual",
        occasions: str = "casual",
        material: str = "",
        fit: str = "regular",
        brand: str = "",
        sizes: str = "",
        phash: str = "",
        owned: int = 1,
    ) -> Garment:
        """Insert a user-added garment (sensible scoring defaults unless given)."""
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO garments
                   (user_id, name, category, color_hex, color_tags, warmth, waterproof,
                    formality, occasions, material, fit, brand, sizes, phash, owned, image_path)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (user_id, name, category, color_hex or "", color_tags or "",
                 warmth, waterproof, formality or "casual", occasions or "casual",
                 material or "", fit or "regular", brand or "", sizes or "",
                 phash or "", 1 if owned else 0, ""),
            )
            self._conn.commit()
            gid = cur.lastrowid
            row = self._conn.execute(
                "SELECT * FROM garments WHERE user_id=? AND id=?", (user_id, gid)
            ).fetchone()
        return self._row_to_garment(row)

    def update_image(self, user_id: int, garment_id: int, image_path: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE garments SET image_path=? WHERE user_id=? AND id=?",
                (image_path, user_id, garment_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def update(self, user_id: int, garment_id: int, **fields: Any) -> bool:
        """Update arbitrary garment columns (caller validates the values).
        Field names must be real columns on `garments`."""
        if not fields:
            return False
        sets = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE garments SET {sets} WHERE user_id=? AND id=?",
                (*fields.values(), user_id, garment_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def delete(self, user_id: int, garment_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM garments WHERE user_id=? AND id=?", (user_id, garment_id)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def _row_to_garment(self, row: Any) -> Garment:
        return Garment(
            id=row["id"], user_id=row["user_id"], name=row["name"],
            category=row["category"], color_hex=row["color_hex"] or "",
            color_tags=row["color_tags"] or "", warmth=row["warmth"],
            waterproof=row["waterproof"], formality=row["formality"],
            occasions=row["occasions"] or "", material=row["material"] or "",
            fit=row["fit"] or "regular",
            brand=row["brand"] or "", sizes=row["sizes"] or "",
            phash=row["phash"] if "phash" in row.keys() else "",
            last_worn=row["last_worn"],
            wear_count=row["wear_count"], rating=row["rating"] or 0,
            owned=row["owned"] if "owned" in row.keys() else 1,
            created_at=row["created_at"] if "created_at" in row.keys() else "",
            image_path=row["image_path"] or "",
        )
