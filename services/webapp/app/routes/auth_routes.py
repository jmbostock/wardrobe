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
    user = auth.authenticate(req.email, req.password)
    if user is None:
        raise HTTPException(401, "invalid email or password")
    token = auth.create_session(user["id"])
    return {"token": token, "user": auth.public_user(user)}


@router.post("/api/auth/logout")
def logout(
    user: dict = Depends(get_current_user), authorization: str = Header(default="")
) -> dict:
    auth.delete_session(authorization[7:])
    return {"ok": True}


@router.get("/api/auth/me")
def me(user: dict = Depends(get_current_user)) -> dict:
    return {"user": auth.public_user(user)}
