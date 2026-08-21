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
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from . import db

ITERATIONS = 200_000
SESSION_TTL_DAYS = int(os.getenv("SESSION_TTL_DAYS", "30"))
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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


def create_user(email: str, password: str) -> dict[str, Any]:
    conn = db.init()
    email = email.strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 200:
        raise AuthError("enter a valid email address")
    if len(password) < 8:
        raise AuthError("password must be at least 8 characters")
    salt, digest = _hash_password(password)
    with db.lock():
        try:
            cur = conn.execute(
                "INSERT INTO users (email, password_salt, password_hash) VALUES (?,?,?)",
                (email, salt, digest),
            )
        except sqlite3.IntegrityError as e:
            raise AuthError("email already registered") from e
        conn.commit()
        user_id = cur.lastrowid
    # NOTE: no seed wardrobe — users start empty and add clothes in the
    # Wardrobe tab (generic seed was removed 2026-08-21).
    return get_user(user_id)  # type: ignore[return-value]


def get_user(user_id: int) -> dict[str, Any] | None:
    conn = db.init()
    with db.lock():
        row = conn.execute(
            "SELECT id, email, lat, lon, created_at FROM users WHERE id=?", (user_id,)
        ).fetchone()
    return dict(row) if row else None


def authenticate(email: str, password: str) -> dict[str, Any] | None:
    conn = db.init()
    email = email.strip().lower()
    with db.lock():
        row = conn.execute(
            "SELECT * FROM users WHERE email=?", (email,)
        ).fetchone()
    if row is None or not _verify_password(password, row["password_salt"], row["password_hash"]):
        return None
    return {
        "id": row["id"], "email": row["email"],
        "lat": row["lat"], "lon": row["lon"], "created_at": row["created_at"],
    }


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
            """SELECT u.id, u.email, u.lat, u.lon, u.created_at, s.expires_at
               FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token = ?""",
            (token,),
        ).fetchone()
    if row is None:
        return None
    if row["expires_at"] and row["expires_at"] < _now_iso():
        delete_session(token)
        return None
    return {
        "id": row["id"], "email": row["email"],
        "lat": row["lat"], "lon": row["lon"], "created_at": row["created_at"],
    }


def delete_session(token: str) -> None:
    conn = db.init()
    with db.lock():
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()


def change_password(user_id: int, current_password: str, new_password: str) -> None:
    """Verify the current password, then set the new one. Raises AuthError on failure."""
    conn = db.init()
    with db.lock():
        row = conn.execute(
            "SELECT password_salt, password_hash FROM users WHERE id=?", (user_id,)
        ).fetchone()
    if row is None or not _verify_password(current_password, row["password_salt"], row["password_hash"]):
        raise AuthError("current password is incorrect")
    if len(new_password) < 8:
        raise AuthError("new password must be at least 8 characters")
    salt, digest = _hash_password(new_password)
    with db.lock():
        conn.execute(
            "UPDATE users SET password_salt=?, password_hash=? WHERE id=?",
            (salt, digest, user_id),
        )
        conn.commit()


def set_location(user_id: int, lat: float, lon: float) -> None:
    conn = db.init()
    with db.lock():
        conn.execute("UPDATE users SET lat=?, lon=? WHERE id=?", (lat, lon, user_id))
        conn.commit()
