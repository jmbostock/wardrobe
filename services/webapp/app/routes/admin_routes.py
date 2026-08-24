"""Dev-only admin endpoints.

Gated by DEV_ADMIN_ENABLED (config). Login happens through the normal
/api/auth/login with the `admin` username; these endpoints back the Account
page's dev console:

  GET  /api/admin/users       — real accounts to switch into
  POST /api/admin/impersonate — act AS a real user (as-if-user, live)
  POST /api/admin/test-copy   — refresh the `test` sandbox with a copy of a user
  GET  /api/admin/test        — info about the test sandbox

Admin endpoints are NOT reachable by normal user tokens (get_current_user
requires a real user row; admin tokens use the ADMIN_SENTINEL_ID), and they
return 403 outright when the feature is disabled (i.e. in production).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import admin, auth
from ..deps import get_admin_user

router = APIRouter()


class ImpersonateRequest(BaseModel):
    user_id: int = Field(..., ge=1)


@router.get("/api/admin/users")
def admin_users(_admin: dict = Depends(get_admin_user)) -> dict:
    return {"users": admin.list_users()}


@router.get("/api/admin/test")
def admin_test_info(_admin: dict = Depends(get_admin_user)) -> dict:
    info = admin.test_copy_info()
    if not info.get("exists"):
        raise HTTPException(404, "test account not found (DEV_ADMIN_ENABLED=1 creates it)")
    return info


@router.get("/api/admin/test/snapshots")
def admin_test_snapshots(_admin: dict = Depends(get_admin_user)) -> dict:
    """Frozen snapshot catalog (which real users have non-live copies)."""
    return {"snapshots": admin.list_snapshots()}


@router.post("/api/admin/test/snapshot-all")
def admin_snapshot_all(_admin: dict = Depends(get_admin_user)) -> dict:
    """Snapshot every real user so the test sandbox can switch between them."""
    return admin.snapshot_all_users()


@router.post("/api/admin/impersonate")
def admin_impersonate(req: ImpersonateRequest, _admin: dict = Depends(get_admin_user)) -> dict:
    """Act AS a real user: the returned token resolves to that user's row, so
    every API call behaves exactly as if that user made it (see AND adjust
    everything). Dev-only; prod never enables it."""
    user = auth.get_user(req.user_id)
    if user is None:
        raise HTTPException(404, "no such user")
    token = auth.create_impersonation_session(req.user_id)
    return {"token": token, "dev": {"acting_as": True, "email": user["email"]}}


@router.post("/api/admin/test-copy")
def admin_test_copy(req: ImpersonateRequest, _admin: dict = Depends(get_admin_user)) -> dict:
    """Refresh the `test` sandbox with a fresh copy of a real user's data
    ("copy it now"). The copy is fully separate — test can change it freely
    without any live adjustments to the real account."""
    try:
        info = admin.copy_into_test(req.user_id)
    except admin.AdminError as ex:
        raise HTTPException(400, str(ex)) from ex
    return {"ok": True, "copied_from": info["from_email"]}
