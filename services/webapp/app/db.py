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
    rating      INTEGER NOT NULL DEFAULT 0,  -- user rating 0..10 (0 = unrated)
    owned       INTEGER NOT NULL DEFAULT 1, -- 1 = own it, 0 = to buy / wishlist
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    image_path  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_garments_user ON garments(user_id);

CREATE TABLE IF NOT EXISTS outfits (
    id              INTEGER PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    garment_ids     TEXT NOT NULL DEFAULT '[]',  -- JSON array of garment ids
    result_url      TEXT NOT NULL DEFAULT '',    -- saved try-on render, if any
    motion_url      TEXT NOT NULL DEFAULT '',    -- SVD animated clip of the render
    person_photo_id INTEGER NOT NULL DEFAULT 0,  -- source person photo id (0 = upload)
    person_url      TEXT NOT NULL DEFAULT '',    -- snapshot of the exact source person image
    rating          INTEGER NOT NULL DEFAULT 0,  -- user rating 0..10 (0 = unrated)
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_outfits_user ON outfits(user_id);

CREATE TABLE IF NOT EXISTS clips (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    prompt_id  TEXT NOT NULL DEFAULT '',    -- ComfyUI prompt id for the SVD job
    status     TEXT NOT NULL DEFAULT 'queued',  -- queued|running|done|error
    result_url TEXT NOT NULL DEFAULT '',    -- /api/uploads/... webp once done
    error      TEXT NOT NULL DEFAULT '',
    outfit_id  INTEGER NOT NULL DEFAULT 0,  -- outfit this motion is attached to
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_clips_user ON clips(user_id);

CREATE TABLE IF NOT EXISTS chat_sessions (
    id         TEXT PRIMARY KEY,           -- UUID
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    messages   TEXT NOT NULL DEFAULT '[]', -- JSON [{role, content}]
    context    TEXT NOT NULL DEFAULT '{}', -- JSON snapshot {weather, outfit, activity}
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_chat_user ON chat_sessions(user_id);
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
    # rating columns (added 2026-08-21) for garments + outfits
    gcols = {r[1] for r in conn.execute("PRAGMA table_info(garments)").fetchall()}
    if "rating" not in gcols:
        conn.execute("ALTER TABLE garments ADD COLUMN rating INTEGER NOT NULL DEFAULT 0")
    if "owned" not in gcols:
        conn.execute("ALTER TABLE garments ADD COLUMN owned INTEGER NOT NULL DEFAULT 1")
    # brand + sizes (added 2026-08-22) — auto-filled from product pages / AI tag reads
    if "brand" not in gcols:
        conn.execute("ALTER TABLE garments ADD COLUMN brand TEXT NOT NULL DEFAULT ''")
    if "sizes" not in gcols:
        conn.execute("ALTER TABLE garments ADD COLUMN sizes TEXT NOT NULL DEFAULT ''")
    if "phash" not in gcols:
        conn.execute("ALTER TABLE garments ADD COLUMN phash TEXT NOT NULL DEFAULT ''")
    if "color_sig" not in gcols:
        conn.execute("ALTER TABLE garments ADD COLUMN color_sig TEXT NOT NULL DEFAULT ''")
    # SQLite's ALTER TABLE only allows constant defaults (datetime('now') works in
    # CREATE TABLE but not ADD COLUMN) — add with '' then backfill existing rows.
    if "created_at" not in gcols:
        conn.execute("ALTER TABLE garments ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")
        conn.execute("UPDATE garments SET created_at = datetime('now') WHERE created_at = ''")
    ocols = {r[1] for r in conn.execute("PRAGMA table_info(outfits)").fetchall()}
    if "rating" not in ocols:
        conn.execute("ALTER TABLE outfits ADD COLUMN rating INTEGER NOT NULL DEFAULT 0")
    if "motion_url" not in ocols:
        conn.execute("ALTER TABLE outfits ADD COLUMN motion_url TEXT NOT NULL DEFAULT ''")
    if "person_photo_id" not in ocols:
        conn.execute("ALTER TABLE outfits ADD COLUMN person_photo_id INTEGER NOT NULL DEFAULT 0")
    if "person_url" not in ocols:
        conn.execute("ALTER TABLE outfits ADD COLUMN person_url TEXT NOT NULL DEFAULT ''")
    # clips table (added 2026-08-22) for async SVD motion generation
    conn.execute(
        """CREATE TABLE IF NOT EXISTS clips (
            id         INTEGER PRIMARY KEY,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            prompt_id  TEXT NOT NULL DEFAULT '',
            status     TEXT NOT NULL DEFAULT 'queued',
            result_url TEXT NOT NULL DEFAULT '',
            error      TEXT NOT NULL DEFAULT '',
            outfit_id  INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_clips_user ON clips(user_id)")
    # chat_sessions table (added 2026-08-23) for DeepSeek stylist chat
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_sessions (
            id         TEXT PRIMARY KEY,
            user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            messages   TEXT NOT NULL DEFAULT '[]',
            context    TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_user ON chat_sessions(user_id)")


def lock() -> threading.Lock:
    return _lock
