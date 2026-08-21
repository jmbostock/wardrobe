"""Shared FastAPI dependencies."""
from __future__ import annotations

from fastapi import Header, HTTPException

from . import auth


def get_current_user(authorization: str = Header(default="")) -> dict:
    """Resolve the Bearer token to a user dict, or raise 401.

    This is the single auth boundary — swap it for OIDC/SSO later without
    touching every route (see docs/architecture.md decision #7).
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    user = auth.get_user_by_token(authorization[7:])
    if user is None:
        raise HTTPException(401, "invalid or expired token", headers={"WWW-Authenticate": "Bearer"})
    return user
