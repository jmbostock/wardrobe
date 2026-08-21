"""Local accounts + bearer session tokens (stdlib-only, portable).

- Passwords: PBKDF2-HMAC-SHA256, 200k iterations, per-user random salt.
- Sessions: opaque random tokens with expiry, stored in the shared db.
- OIDC/SSO (Authelia/Keycloak) is a possible phase-3 swap — keep the same
  `get_user_by_token` boundary so callers don't change.

Endpoints that need auth use `main.get_current_user` (FastAPI dependency),
which calls `get_user_by_token`.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from . import db

ITERATIONS = 200_000
SESSION_TTL_DAYS = int(os.getenv("SESSION_TTL_DAYS", "30"))


class AuthError(Exception):
    """Raised for user-facing auth failures (bad creds, taken username...)."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _expires_iso() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=SESSION_TTL_DAYS)).isoformat(
        timespec="seconds"
    )


def _hash_password(password: str) -> tuple[str, str]:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), ITERATIONS
    )
    return salt, digest.hex()


def _verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt_hex), ITERATIONS
    )
    return hmac.compare_digest(digest.hex(), hash_hex)


def create_user(username: str, password: str) -> dict[str, Any]:
    conn = db.init()
    username = username.strip()
    if not 3 <= len(username) <= 40:
        raise AuthError("username must be 3-40 characters")
    if len(password) < 8:
        raise AuthError("password must be at least 8 characters")
    salt, digest = _hash_password(password)
    with db.lock():
        try:
            cur = conn.execute(
                "INSERT INTO users (username, password_salt, password_hash) VALUES (?,?,?)",
                (username, salt, digest),
            )
        except sqlite3.IntegrityError as e:
            raise AuthError("username already taken") from e
        conn.commit()
        user_id = cur.lastrowid
    # every user gets a copy of the seed wardrobe
    from .wardrobe import Wardrobe

    Wardrobe().seed_for_user(user_id)
    return get_user(user_id)  # type: ignore[return-value]


def get_user(user_id: int) -> dict[str, Any] | None:
    conn = db.init()
    with db.lock():
        row = conn.execute(
            "SELECT id, username, created_at FROM users WHERE id=?", (user_id,)
        ).fetchone()
    return dict(row) if row else None


def authenticate(username: str, password: str) -> dict[str, Any] | None:
    conn = db.init()
    with db.lock():
        row = conn.execute(
            "SELECT * FROM users WHERE username=?", (username.strip(),)
        ).fetchone()
    if row is None or not _verify_password(password, row["password_salt"], row["password_hash"]):
        return None
    return {"id": row["id"], "username": row["username"], "created_at": row["created_at"]}


def create_session(user_id: int) -> str:
    conn = db.init()
    token = secrets.token_urlsafe(32)
    with db.lock():
        conn.execute(
            "INSERT INTO sessions (token, user_id, expires_at) VALUES (?,?,?)",
            (token, user_id, _expires_iso()),
        )
        conn.commit()
    return token


def get_user_by_token(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    conn = db.init()
    with db.lock():
        row = conn.execute(
            """SELECT u.id, u.username, u.created_at, s.expires_at
               FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token = ?""",
            (token,),
        ).fetchone()
    if row is None:
        return None
    if row["expires_at"] and row["expires_at"] < _now_iso():
        delete_session(token)
        return None
    return {"id": row["id"], "username": row["username"], "created_at": row["created_at"]}


def delete_session(token: str) -> None:
    conn = db.init()
    with db.lock():
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()
