"""Shared sqlite connection + schema for altacloset.

Single connection (check_same_thread=False) + one lock, shared by `wardrobe`,
`auth`, and anything else that touches the DB. Safe for FastAPI's threadpool.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .config import settings

DB_PATH = Path(settings.data_dir) / "db" / "altacloset.db"

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY,
    email          TEXT NOT NULL UNIQUE,
    password_salt  TEXT NOT NULL,
    password_hash  TEXT NOT NULL,
    lat            REAL,                -- per-user location; NULL -> default (San Mateo, CA 94403)
    lon            REAL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS photos (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    filename    TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',   -- shown in the try-on picker
    is_default  INTEGER NOT NULL DEFAULT 0, -- only one per user
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_photos_user ON photos(user_id);

CREATE TABLE IF NOT EXISTS garments (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    category    TEXT NOT NULL,          -- top|bottom|dress|outerwear|footwear|accessory
    color_hex   TEXT,
    color_tags  TEXT,                   -- comma list e.g. "navy,dark"
    warmth      INTEGER NOT NULL DEFAULT 3,  -- 1 (thin) .. 5 (heavy)
    waterproof  INTEGER NOT NULL DEFAULT 0,
    formality   TEXT NOT NULL DEFAULT 'casual', -- casual|smart-casual|business|formal
    occasions   TEXT,                   -- comma list: office,date,hiking,event,...
    material    TEXT,
    fit         TEXT DEFAULT 'regular',
    last_worn   TEXT,
    wear_count  INTEGER NOT NULL DEFAULT 0,
    image_path  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_garments_user ON garments(user_id);

CREATE TABLE IF NOT EXISTS outfits (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    garment_ids TEXT NOT NULL DEFAULT '[]',  -- JSON array of garment ids
    result_url  TEXT NOT NULL DEFAULT '',    -- saved try-on render, if any
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_outfits_user ON outfits(user_id);
"""


def init() -> sqlite3.Connection:
    """Return the shared connection, creating it + schema on first use."""
    global _conn
    with _lock:
        if _conn is None:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA busy_timeout=5000")
            _conn.executescript(SCHEMA)
            _migrate(_conn)
            _conn.commit()
    return _conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive migrations for DBs created by older schemas (no reset needed)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(photos)").fetchall()}
    if "description" not in cols:
        conn.execute("ALTER TABLE photos ADD COLUMN description TEXT NOT NULL DEFAULT ''")
    # outfits table (added 2026-08-21)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS outfits (
            id          INTEGER PRIMARY KEY,
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name        TEXT NOT NULL,
            garment_ids TEXT NOT NULL DEFAULT '[]',
            result_url  TEXT NOT NULL DEFAULT '',
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_outfits_user ON outfits(user_id)")
    # result_url column (added 2026-08-21) for existing outfits tables
    ocols = {r[1] for r in conn.execute("PRAGMA table_info(outfits)").fetchall()}
    if "result_url" not in ocols:
        conn.execute("ALTER TABLE outfits ADD COLUMN result_url TEXT NOT NULL DEFAULT ''")


def lock() -> threading.Lock:
    return _lock
