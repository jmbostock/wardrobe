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
SESSION_TTL_DAYS = int(os.getenv("SESSION_TTL_DAYS", "180"))
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Sentinel user id for admin sessions (no real user has id 0). Admin tokens can
# never be used against normal user endpoints — get_user_by_token requires a
# real user row, which id 0 doesn't have.
ADMIN_SENTINEL_ID = 0


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


def make_unusable_credentials() -> tuple[str, str]:
    """A salt+hash for an account nobody can log into (dev-copy snapshot users).
    The plaintext is a random secret that is never stored anywhere."""
    return _hash_password(secrets.token_urlsafe(24))


def _dev_account_spec() -> list[tuple[str, str, str, str]]:
    """(role, login, email, password) for the two dev accounts, only when the
    dev feature is enabled. Defaults are the requested admin/test creds."""
    from .config import settings

    if not settings.dev_admin_enabled:
        return []
    return [
        ("admin", settings.dev_admin_login, settings.dev_admin_email, settings.dev_admin_password),
        ("test", settings.dev_test_login, settings.dev_test_email, settings.dev_test_password),
    ]


def ensure_dev_accounts() -> None:
    """Create the `admin` and `test` dev accounts if they don't exist. Called at
    app startup (and lazily on login) so the two dev logins always work once
    DEV_ADMIN_ENABLED=1. Both are ordinary user rows, just tagged with a role."""
    from .config import settings

    if not settings.dev_admin_enabled:
        return
    conn = db.init()
    for role, login, email, password in _dev_account_spec():
        if email and len(password) >= 8:
            try:
                create_user(email, password)
            except AuthError:
                pass  # already exists
            conn.execute("UPDATE users SET role=? WHERE email=?", (role, email))
            conn.commit()


def dev_login(identifier: str, password: str) -> dict[str, Any] | None:
    """Authenticate a dev account by USERNAME (e.g. `admin`, `test`) rather than
    email. Only active when the dev feature is enabled. Returns the user dict
    (with role) or None on bad credentials / disabled feature."""
    ident = (identifier or "").strip().lower()
    for role, login, email, want_pw in _dev_account_spec():
        if ident == login.strip().lower():
            import hmac

            if not hmac.compare_digest((password or "").encode(), want_pw.encode()):
                return None
            return get_user_by_email(email)
    return None


def get_user_by_email(email: str) -> dict[str, Any] | None:
    """Look up a user by email (used to resolve dev accounts)."""
    conn = db.init()
    with db.lock():
        row = conn.execute(
            "SELECT id, email, lat, lon, location, profile, derived_profile, created_at, role "
            "FROM users WHERE email=?",
            (email.strip().lower(),),
        ).fetchone()
    if not row:
        return None
    u = dict(row)
    u["profile"] = _json_col(u.get("profile"))
    u["derived_profile"] = _json_col(u.get("derived_profile"))
    return u


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
    shown in the UI. Session bookkeeping keys (session_kind / snapshot_of) are
    internal and never sent to the browser in the user payload.
    """
    u = dict(user)
    u.pop("derived_profile", None)
    u.pop("session_kind", None)
    u.pop("snapshot_of", None)
    return u

def get_user(user_id: int) -> dict[str, Any] | None:
    conn = db.init()
    with db.lock():
        row = conn.execute(
            "SELECT id, email, lat, lon, location, profile, derived_profile, created_at, role FROM users WHERE id=?",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    u = dict(row)
    u["profile"] = _json_col(u.get("profile"))
    u["derived_profile"] = _json_col(u.get("derived_profile"))
    return u


def get_dev_user(role: str) -> dict[str, Any] | None:
    """Find a dev account by role ('admin' | 'test'). Used to resolve the test
    sandbox account for copying data into it."""
    conn = db.init()
    with db.lock():
        row = conn.execute(
            "SELECT id, email, role, test_source FROM users WHERE role=?", (role,)
        ).fetchone()
    return dict(row) if row else None


def authenticate(email: str, password: str) -> dict[str, Any] | None:
    """Log a user in. Accepts an email OR (when the dev feature is on) a dev
    username like `admin` / `test`. Returns the user dict (with `role`) or None."""
    conn = db.init()
    email = email.strip().lower()
    # dev accounts log in by username (admin / test), not email
    dev_user = dev_login(email, password)
    if dev_user is not None:
        sharing.ensure_family(dev_user["id"])
        return dev_user
    with db.lock():
        row = conn.execute(
            "SELECT * FROM users WHERE email=?", (email,)
        ).fetchone()
    if row is None or not _verify_password(password, row["password_salt"], row["password_hash"]):
        return None
    # hidden snapshot users (dev test-snapshots) can never log in
    if (row["role"] or "user") == "snapshot":
        return None
    sharing.ensure_family(row["id"])  # keep returning users in the Family group
    return {
        "id": row["id"], "email": row["email"],
        "lat": row["lat"], "lon": row["lon"], "location": row["location"],
        "created_at": row["created_at"], "role": row["role"] or "user",
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


def create_admin_session() -> str:
    """Issue a dev-admin bearer token (kind='admin'). Only ever used by the
    dev-only admin endpoints; cannot resolve to a user via get_user_by_token."""
    conn = db.init()
    token = secrets.token_urlsafe(32)
    with db.lock():
        conn.execute(
            "INSERT INTO sessions (token, user_id, kind, expires_at) VALUES (?,?,?,?)",
            (token, ADMIN_SENTINEL_ID, "admin", _expires_iso()),
        )
        conn.commit()
    return token


def create_impersonation_session(user_id: int) -> str:
    """Issue a bearer token that lets the admin act AS a real user (kind=
    'impersonate'). The session resolves to that user's row, so every API call
    behaves exactly as if that user made it — see and adjust everything. This
    only runs on the dev instance (gated by DEV_ADMIN_ENABLED)."""
    conn = db.init()
    token = secrets.token_urlsafe(32)
    with db.lock():
        conn.execute(
            "INSERT INTO sessions (token, user_id, kind, snapshot_of, expires_at) "
            "VALUES (?,?,?,?,?)",
            (token, user_id, "impersonate", user_id, _expires_iso()),
        )
        conn.commit()
    return token


def get_session(token: str | None) -> dict[str, Any] | None:
    """Look up a session row (any kind) by token, enforcing expiry. Returns
    None for missing/expired tokens. Used by the admin dependency and the /me
    endpoint — callers must check `kind`."""
    if not token:
        return None
    conn = db.init()
    with db.lock():
        row = conn.execute(
            "SELECT token, user_id, kind, snapshot_of, expires_at FROM sessions WHERE token=?",
            (token,),
        ).fetchone()
    if row is None:
        return None
    if row["expires_at"] and row["expires_at"] < _now_iso():
        delete_session(token)
        return None
    return dict(row)


def get_user_by_token(token: str | None) -> dict[str, Any] | None:
    """Resolve a bearer token to a user dict, or None.

    Only real 'user' and 'impersonate' sessions resolve to a user (admin tokens
    use the ADMIN_SENTINEL_ID which has no user row → None). For impersonate
    sessions the returned dict also carries `session_kind` / `snapshot_of` so
    callers know this is a dev copy of a real account."""
    if not token:
        return None
    conn = db.init()
    with db.lock():
        row = conn.execute(
            """SELECT u.id, u.email, u.lat, u.lon, u.location, u.profile,
                      u.derived_profile, u.created_at, u.role, s.expires_at, s.kind, s.snapshot_of
               FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token = ?""",
            (token,),
        ).fetchone()
    if row is None:
        return None
    if row["expires_at"] and row["expires_at"] < _now_iso():
        delete_session(token)
        return None
    if (row["role"] or "user") == "snapshot":
        return None  # hidden snapshot users can never hold a usable session
    return {
        "id": row["id"], "email": row["email"],
        "lat": row["lat"], "lon": row["lon"], "location": row["location"],
        "created_at": row["created_at"], "role": row["role"] or "user",
        "profile": _json_col(row["profile"]), "derived_profile": _json_col(row["derived_profile"]),
        "session_kind": row["kind"] or "user",
        "snapshot_of": row["snapshot_of"],
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


def set_location(user_id: int, lat: float, lon: float, name: str | None = None) -> None:
    conn = db.init()
    with db.lock():
        conn.execute(
            "UPDATE users SET lat=?, lon=?, location=? WHERE id=?",
            (lat, lon, name, user_id),
        )
        conn.commit()
