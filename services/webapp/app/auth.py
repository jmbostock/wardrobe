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
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from . import db, sharing

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
    sharing.ensure_family(user_id)  # every user is a member of the Family group
    # NOTE: no seed wardrobe — users start empty and add clothes in the
    # Wardrobe tab (generic seed was removed 2026-08-21).
    return get_user(user_id)  # type: ignore[return-value]


def _json_col(value: str | None) -> dict:
    """Parse a JSON text column, tolerating NULL/empty/malformed."""
    if not value:
        return {}
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return {}


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    """User dict safe to send to the browser — includes the raw `profile` bio
    but drops the computed `derived_profile`. The derived profile is server-side
    only (stylist/recommender input); per user request it is tracked but never
    shown in the UI.
    """
    u = dict(user)
    u.pop("derived_profile", None)
    return u

def get_user(user_id: int) -> dict[str, Any] | None:
    conn = db.init()
    with db.lock():
        row = conn.execute(
            "SELECT id, email, lat, lon, profile, derived_profile, created_at FROM users WHERE id=?",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    u = dict(row)
    u["profile"] = _json_col(u.get("profile"))
    u["derived_profile"] = _json_col(u.get("derived_profile"))
    return u


def authenticate(email: str, password: str) -> dict[str, Any] | None:
    conn = db.init()
    email = email.strip().lower()
    with db.lock():
        row = conn.execute(
            "SELECT * FROM users WHERE email=?", (email,)
        ).fetchone()
    if row is None or not _verify_password(password, row["password_salt"], row["password_hash"]):
        return None
    sharing.ensure_family(row["id"])  # keep returning users in the Family group
    return {
        "id": row["id"], "email": row["email"],
        "lat": row["lat"], "lon": row["lon"], "created_at": row["created_at"],
        "profile": _json_col(row["profile"]), "derived_profile": _json_col(row["derived_profile"]),
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
            """SELECT u.id, u.email, u.lat, u.lon, u.profile, u.derived_profile,
                      u.created_at, s.expires_at
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
        "profile": _json_col(row["profile"]), "derived_profile": _json_col(row["derived_profile"]),
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
