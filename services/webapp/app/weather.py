"""Weather source. Open-Meteo (no API key) by default, Home Assistant override.

WEATHER_SOURCE=openmeteo    (default; needs internet)
WEATHER_SOURCE=homeassistant (uses HA_URL / HA_TOKEN / HA_WEATHER_ENTITY)
"""
from __future__ import annotations

import os

import httpx

from .config import settings
from .recommender import Weather


DEFAULT_LOCATION = {"lat": 37.5396, "lon": -122.2974, "name": "San Mateo, CA 94403"}


def fetch(lat: float | None = None, lon: float | None = None) -> Weather:
    """Current weather. Defaults to San Mateo, CA 94403 unless coords given."""
    lat = float(lat) if lat is not None else DEFAULT_LOCATION["lat"]
    lon = float(lon) if lon is not None else DEFAULT_LOCATION["lon"]
    if settings.weather_source == "homeassistant":
        return _fetch_ha()
    return _fetch_openmeteo(lat, lon)


def _fetch_openmeteo(lat: float, lon: float) -> Weather:
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": (
            "temperature_2m,apparent_temperature,relative_humidity_2m,"
            "precipitation,weather_code,wind_speed_10m"
        ),
    }
    r = httpx.get(url, params=params, timeout=10)
    r.raise_for_status()
    cur = r.json()["current"]
    return Weather(
        temp_c=cur["temperature_2m"],
        feels_like_c=cur["apparent_temperature"],
        condition=_code_to_condition(cur["weather_code"], cur.get("precipitation", 0)),
        wind_kph=cur.get("wind_speed_10m", 0.0),
        humidity=int(cur.get("relative_humidity_2m", 50)),
        uv_index=0.0,  # separate daily endpoint; 0 keeps MVP simple
    )


def geocode(query: str) -> dict | None:
    """Resolve a free-text location (zip, city, 'lat,lon') to coords via Open-Meteo."""
    query = query.strip()
    if not query:
        return None
    # allow explicit "lat,lon"
    if "," in query:
        try:
            lat, lon = (float(x) for x in query.split(",", 1))
            return {"lat": lat, "lon": lon, "name": query, "country": ""}
        except ValueError:
            pass
    url = "https://geocoding-api.open-meteo.com/v1/search"
    params = {"name": query, "count": 1, "language": "en", "format": "json"}
    try:
        r = httpx.get(url, params=params, timeout=10)
        r.raise_for_status()
        results = (r.json() or {}).get("results") or []
    except Exception:
        return None
    if not results:
        return None
    best = results[0]
    return {
        "lat": best["latitude"],
        "lon": best["longitude"],
        "name": best.get("name", query),
        "country": best.get("country", ""),
    }


def _fetch_ha() -> Weather:
    if not settings.ha_url:
        raise RuntimeError("WEATHER_SOURCE=homeassistant requires HA_URL")
    url = f"{settings.ha_url}/api/states/{settings.ha_weather_entity}"
    headers = {"Authorization": f"Bearer {settings.ha_token}"}
    r = httpx.get(url, headers=headers, timeout=10)
    r.raise_for_status()
    st = r.json()
    attrs = st.get("attributes", {})
    state = st.get("state", "")
    condition = state  # HA weather states: clear, cloudy, rainy, snowy, ...
    if condition in ("rainy", "pouring", "snowy", "snowy-rainy"):
        condition = "rain" if "snow" not in condition else "snow"
    return Weather(
        temp_c=float(attrs.get("temperature", 0.0)),
        feels_like_c=float(attrs.get("apparent_temperature") or attrs.get("temperature", 0.0)),
        condition=condition,
        wind_kph=float(attrs.get("wind_speed", 0.0)),
        humidity=int(attrs.get("humidity", 50)),
        uv_index=float(attrs.get("uv_index", 0.0) or 0.0),
    )


def _code_to_condition(code: int, precip: float) -> str:
    if code == 0:
        return "clear"
    if code in (1, 2, 3):
        return "cloudy"
    if code in (45, 48):
        return "fog"
    if code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
        return "rain"
    if code in (71, 73, 75, 77, 85, 86):
        return "snow"
    if code in (95, 96, 99):
        return "thunderstorm"
    return "cloudy"
