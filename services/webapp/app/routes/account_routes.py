"""Account endpoints — profile, location, password."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import auth, profile, weather
from ..deps import get_current_user

router = APIRouter()


class PasswordRequest(BaseModel):
    current_password: str
    new_password: str


class LocationRequest(BaseModel):
    location: str = Field(..., min_length=1, max_length=120)


class ProfileRequest(BaseModel):
    profile: dict = Field(default_factory=dict)


@router.get("/api/account")
def account(user: dict = Depends(get_current_user)) -> dict:
    return {
        "user": auth.public_user(user),
        # optional style bio — the computed derived profile stays server-side
        "profile": user.get("profile", {}),
        "location": {
            "lat": user["lat"] if user["lat"] is not None else weather.DEFAULT_LOCATION["lat"],
            "lon": user["lon"] if user["lon"] is not None else weather.DEFAULT_LOCATION["lon"],
            "label": "San Mateo, CA 94403 (default)" if user["lat"] is None else None,
        },
        "default_location": weather.DEFAULT_LOCATION,
    }


@router.post("/api/account/profile")
def save_profile(req: ProfileRequest, user: dict = Depends(get_current_user)) -> dict:
    """Save the optional style bio; recompute the computed derived profile."""
    saved = profile.save_profile(user["id"], req.profile)
    return {"ok": True, "profile": saved}


@router.post("/api/account/password")
def change_password(req: PasswordRequest, user: dict = Depends(get_current_user)) -> dict:
    try:
        auth.change_password(user["id"], req.current_password, req.new_password)
    except auth.AuthError as ex:
        raise HTTPException(400, str(ex)) from ex
    return {"ok": True}


@router.post("/api/account/location")
def set_location(req: LocationRequest, user: dict = Depends(get_current_user)) -> dict:
    loc = weather.geocode(req.location)
    if loc is None:
        raise HTTPException(400, f"could not resolve location: {req.location}")
    auth.set_location(user["id"], loc["lat"], loc["lon"])
    return {"ok": True, "location": loc}
