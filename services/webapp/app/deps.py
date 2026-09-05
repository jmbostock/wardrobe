"""Shared FastAPI dependencies."""
from __future__ import annotations

from fastapi import Header, HTTPException

from . import auth


def get_current_user(authorization: str = Header(default="")) -> dict:
    """Resolve the Bearer token to a user dict, or raise 401.

    This is the single auth boundary — swap it for OIDC/SSO later without
    touching every route (see docs/architecture.md decision #7).

    A dev-`admin` acting-as token (kind='impersonate') resolves to that user's
    row, so every normal endpoint behaves exactly as if that user made the
    call — dev-only, never in production.
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    user = auth.get_user_by_token(authorization[7:])
    if user is None:
        raise HTTPException(401, "invalid or expired token", headers={"WWW-Authenticate": "Bearer"})
    return user


def get_admin_user(authorization: str = Header(default="")) -> dict:
    """Dev-admin-only dependency. Requires the feature to be enabled AND a
    kind='admin' session token. Refuses cleanly when disabled (production)."""
    from . import admin

    if not admin.dev_admin_enabled():
        raise HTTPException(403, "dev admin is not enabled on this instance")
    token = authorization[7:] if authorization.startswith("Bearer ") else ""
    sess = auth.get_session(token)
    if sess is None or sess["kind"] != "admin":
        raise HTTPException(
            401, "valid dev-admin token required", headers={"WWW-Authenticate": "Bearer"}
        )
    return {"admin": True}
