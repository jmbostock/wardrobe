"""Auth endpoints — register, login, logout, me."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from .. import auth
from ..deps import get_current_user

router = APIRouter()


class AuthRequest(BaseModel):
    email: str = Field(..., min_length=1, max_length=200)
    password: str = Field(..., min_length=1, max_length=200)


@router.post("/api/auth/register")
def register(req: AuthRequest) -> dict:
    try:
        user = auth.create_user(req.email, req.password)
    except auth.AuthError as ex:
        raise HTTPException(400, str(ex)) from ex
    token = auth.create_session(user["id"])
    return {"token": token, "user": auth.public_user(user)}


@router.post("/api/auth/login")
def login(req: AuthRequest) -> dict:
    auth.ensure_dev_accounts()  # dev `admin`/`test` accounts always exist when enabled
    user = auth.authenticate(req.email, req.password)
    if user is None:
        raise HTTPException(401, "invalid email or password")
    # the dev `admin` account gets a kind='admin' session (admin console on the
    # Account page); everyone else gets a normal user session (test included —
    # test is just a user whose data is a copy)
    if user.get("role") == "admin":
        token = auth.create_admin_session()
    else:
        token = auth.create_session(user["id"])
    return {"token": token, "user": auth.public_user(user)}


@router.post("/api/auth/logout")
def logout(
    user: dict = Depends(get_current_user), authorization: str = Header(default="")
) -> dict:
    auth.delete_session(authorization[7:])
    return {"ok": True}


@router.get("/api/auth/me")
def me(authorization: str = Header(default="")) -> dict:
    """Session info for the frontend guard. Returns one of:
      - {"user": {...}}                       normal user session
      - {"user": {...}, "dev": {"test_copy": true}}   the dev `test` sandbox
      - {"admin": True, "user": None}         dev `admin` session (Account console)
      - {"user": {...}, "dev": {"acting_as": {...}}}  admin acting AS a real user
    """
    token = authorization[7:] if authorization.startswith("Bearer ") else ""
    sess = auth.get_session(token)
    if sess is None:
        raise HTTPException(401, "invalid or expired token", headers={"WWW-Authenticate": "Bearer"})
    if sess["kind"] == "admin":
        return {"admin": True, "user": None}
    user = auth.get_user_by_token(token)
    if user is None:
        raise HTTPException(401, "invalid or expired token", headers={"WWW-Authenticate": "Bearer"})
    resp: dict = {"user": auth.public_user(user)}
    if sess["kind"] == "impersonate":
        resp["dev"] = {"acting_as": {"id": user["id"], "email": user["email"]}}
    elif user.get("role") == "test":
        from .. import admin  # lazy: admin imports auth
        resp["dev"] = {"test_copy": True, "copy_of": admin.test_copy_source()}
    return resp
