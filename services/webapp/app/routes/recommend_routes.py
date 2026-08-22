"""Weather + recommendation endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import recommender, weather
from ..deps import get_current_user
from ..recommender import Weather
from ..store import wardrobe

router = APIRouter()


class WeatherIn(BaseModel):
    temp_c: float
    feels_like_c: float | None = None
    condition: str = "clear"
    wind_kph: float = 0.0
    humidity: int = 50
    uv_index: float = 0.0


class RecommendRequest(BaseModel):
    activity: str = Field("casual", description="office, date, hiking, ...")
    prompt: str | None = Field(None, description="free-form style hint")
    owned_only: bool = Field(False, description="only recommend garments you own")
    weather: WeatherIn | None = Field(None, description="omit to auto-fetch")


def _user_coords(user: dict) -> tuple[float, float]:
    lat = user["lat"] if user["lat"] is not None else weather.DEFAULT_LOCATION["lat"]
    lon = user["lon"] if user["lon"] is not None else weather.DEFAULT_LOCATION["lon"]
    return float(lat), float(lon)


@router.get("/api/weather")
def get_weather(user: dict = Depends(get_current_user)) -> dict:
    lat, lon = _user_coords(user)
    try:
        return weather.fetch(lat, lon).to_dict()
    except Exception as ex:  # noqa: BLE001 — surface fetch failures clearly
        raise HTTPException(502, f"weather fetch failed: {ex}") from ex


@router.post("/api/recommend")
def recommend_outfit(req: RecommendRequest, user: dict = Depends(get_current_user)) -> dict:
    if req.weather:
        w = Weather(**req.weather.__dict__)
    else:
        lat, lon = _user_coords(user)
        w = weather.fetch(lat, lon)
    return recommender.recommend(
        w, req.activity, req.prompt, wardrobe=wardrobe, user_id=user["id"],
        owned_only=req.owned_only,
    )
