"""Dev test-sandbox self-service endpoints (role='test' only).

The `test` account is a sandbox whose data is a copy of a real user. These
endpoints let the test user switch which FROZEN snapshot it mirrors — always
non-live (the switch copies FROM the hidden snapshot user INTO the test user;
live data is never written). Only reachable when DEV_ADMIN_ENABLED=1 and only
for the role='test' account.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import admin
from ..deps import get_current_user

router = APIRouter()


def _require_test(user: dict = Depends(get_current_user)) -> dict:
    if not admin.dev_admin_enabled():
        raise HTTPException(403, "dev feature is disabled")
    if (user.get("role") or "user") != "test":
        raise HTTPException(403, "test sandbox account required")
    return user


class SwitchRequest(BaseModel):
    user_id: int = Field(..., ge=1)


@router.get("/api/test/snapshots")
def test_snapshots(_user: dict = Depends(_require_test)) -> dict:
    """Snapshots available to switch to + which one is active."""
    return {"snapshots": admin.list_snapshots()}


@router.post("/api/test/switch")
def test_switch(req: SwitchRequest, _user: dict = Depends(_require_test)) -> dict:
    """Point this test sandbox at another user's frozen snapshot (non-live)."""
    try:
        info = admin.switch_test_to(req.user_id)
    except admin.AdminError as ex:
        raise HTTPException(400, str(ex)) from ex
    return info
