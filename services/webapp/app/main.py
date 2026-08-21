"""altacloset — FastAPI entrypoint (multi-user).

Public:
  GET  /health · GET  /
  POST /api/auth/register → {email,password} → {token, user}
  POST /api/auth/login    → {email,password} → {token, user}
Authenticated (Bearer token):
  POST /api/auth/logout · GET /api/auth/me
  GET  /api/weather (F + C, per-user location) · GET /api/wardrobe · POST /api/recommend
  POST /api/tryon (garment + person photo OR saved photo_id)
  GET  /api/uploads/{name} (owner-only try-on result)
  Account: GET /api/account · POST /api/account/password · POST /api/account/location
  Photos:  GET/POST /api/photos · DELETE /api/photos/{id} · POST /api/photos/{id}/default ·
           GET /api/photos/{id}/image (owner-only)
"""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, photos, recommender, tryon, weather
from .config import settings
from .recommender import Weather
from .wardrobe import Wardrobe

app = FastAPI(title="altacloset", version="0.3.0")

STATIC_DIR = Path(__file__).parent / "static"
UPLOAD_DIR = Path(settings.data_dir) / "uploads"

_wardrobe = Wardrobe()


# --------------------------------------------------------------------------- #
# auth dependency
# --------------------------------------------------------------------------- #
def get_current_user(authorization: str = Header(default="")) -> dict:
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    user = auth.get_user_by_token(authorization[7:])
    if user is None:
        raise HTTPException(401, "invalid or expired token", headers={"WWW-Authenticate": "Bearer"})
    return user


# --------------------------------------------------------------------------- #
# models
# --------------------------------------------------------------------------- #
class AuthRequest(BaseModel):
    email: str = Field(..., min_length=1, max_length=200)
    password: str = Field(..., min_length=1, max_length=200)


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
    weather: WeatherIn | None = Field(None, description="omit to auto-fetch")


class PasswordRequest(BaseModel):
    current_password: str
    new_password: str


class LocationRequest(BaseModel):
    location: str = Field(..., min_length=1, max_length=120)


class PhotoDescriptionRequest(BaseModel):
    description: str = Field("", max_length=200)


# --------------------------------------------------------------------------- #
# public
# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> dict:
    return {"ok": True, "service": "altacloset", "version": "0.3.0"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/auth/register")
def register(req: AuthRequest) -> dict:
    try:
        user = auth.create_user(req.email, req.password)
    except auth.AuthError as ex:
        raise HTTPException(400, str(ex)) from ex
    token = auth.create_session(user["id"])
    return {"token": token, "user": user}


@app.post("/api/auth/login")
def login(req: AuthRequest) -> dict:
    user = auth.authenticate(req.email, req.password)
    if user is None:
        raise HTTPException(401, "invalid email or password")
    token = auth.create_session(user["id"])
    return {"token": token, "user": user}


# --------------------------------------------------------------------------- #
# authenticated — session & account
# --------------------------------------------------------------------------- #
@app.post("/api/auth/logout")
def logout(user: dict = Depends(get_current_user), authorization: str = Header(default="")) -> dict:
    auth.delete_session(authorization[7:])
    return {"ok": True}


@app.get("/api/auth/me")
def me(user: dict = Depends(get_current_user)) -> dict:
    return {"user": user}


@app.get("/api/account")
def account(user: dict = Depends(get_current_user)) -> dict:
    return {
        "user": user,
        "location": {
            "lat": user["lat"] if user["lat"] is not None else weather.DEFAULT_LOCATION["lat"],
            "lon": user["lon"] if user["lon"] is not None else weather.DEFAULT_LOCATION["lon"],
            "label": "San Mateo, CA 94403 (default)" if user["lat"] is None else None,
        },
        "default_location": weather.DEFAULT_LOCATION,
    }


@app.post("/api/account/password")
def change_password(req: PasswordRequest, user: dict = Depends(get_current_user)) -> dict:
    try:
        auth.change_password(user["id"], req.current_password, req.new_password)
    except auth.AuthError as ex:
        raise HTTPException(400, str(ex)) from ex
    return {"ok": True}


@app.post("/api/account/location")
def set_location(req: LocationRequest, user: dict = Depends(get_current_user)) -> dict:
    loc = weather.geocode(req.location)
    if loc is None:
        raise HTTPException(400, f"could not resolve location: {req.location}")
    auth.set_location(user["id"], loc["lat"], loc["lon"])
    return {"ok": True, "location": loc}


# --------------------------------------------------------------------------- #
# authenticated — weather / wardrobe / recommend
# --------------------------------------------------------------------------- #
def _user_coords(user: dict) -> tuple[float, float]:
    lat = user["lat"] if user["lat"] is not None else weather.DEFAULT_LOCATION["lat"]
    lon = user["lon"] if user["lon"] is not None else weather.DEFAULT_LOCATION["lon"]
    return float(lat), float(lon)


@app.get("/api/weather")
def get_weather(user: dict = Depends(get_current_user)) -> dict:
    lat, lon = _user_coords(user)
    try:
        return weather.fetch(lat, lon).to_dict()
    except Exception as ex:  # noqa: BLE001 — surface fetch failures clearly
        raise HTTPException(502, f"weather fetch failed: {ex}") from ex


@app.get("/api/wardrobe")
def list_wardrobe(user: dict = Depends(get_current_user)) -> list[dict]:
    _wardrobe.seed_for_user(user["id"])
    return [g.to_dict() for g in _wardrobe.all(user["id"])]


@app.post("/api/recommend")
def recommend_outfit(req: RecommendRequest, user: dict = Depends(get_current_user)) -> dict:
    if req.weather:
        w = Weather(**req.weather.__dict__)
    else:
        lat, lon = _user_coords(user)
        w = weather.fetch(lat, lon)
    return recommender.recommend(w, req.activity, req.prompt, wardrobe=_wardrobe, user_id=user["id"])


# --------------------------------------------------------------------------- #
# authenticated — photos
# --------------------------------------------------------------------------- #
@app.get("/api/photos")
def list_photos(user: dict = Depends(get_current_user)) -> list[dict]:
    return photos.list(user["id"])


@app.post("/api/photos")
async def upload_photo(person: UploadFile = File(...), user: dict = Depends(get_current_user)) -> dict:
    data = await person.read()
    if not data:
        raise HTTPException(400, "empty image")
    ext = Path(person.filename or "").suffix
    try:
        return photos.upload(user["id"], data, ext)
    except photos.PhotoError as ex:
        raise HTTPException(400, str(ex)) from ex


@app.post("/api/photos/{photo_id}/default")
def set_default_photo(photo_id: int, user: dict = Depends(get_current_user)) -> dict:
    try:
        photos.set_default(user["id"], photo_id)
    except photos.PhotoError as ex:
        raise HTTPException(404, str(ex)) from ex
    return {"ok": True}


@app.patch("/api/photos/{photo_id}")
def update_photo(
    photo_id: int, req: PhotoDescriptionRequest, user: dict = Depends(get_current_user)
) -> dict:
    try:
        return photos.set_description(user["id"], photo_id, req.description)
    except photos.PhotoError as ex:
        raise HTTPException(404, str(ex)) from ex


@app.delete("/api/photos/{photo_id}")
def delete_photo(photo_id: int, user: dict = Depends(get_current_user)) -> dict:
    try:
        photos.delete(user["id"], photo_id)
    except photos.PhotoError as ex:
        raise HTTPException(404, str(ex)) from ex
    return {"ok": True}


@app.get("/api/photos/{photo_id}/image")
def photo_image(photo_id: int, user: dict = Depends(get_current_user)) -> FileResponse:
    try:
        path = photos.photo_path(user["id"], photo_id)
    except photos.PhotoError as ex:
        raise HTTPException(404, str(ex)) from ex
    return FileResponse(path, media_type="image/jpeg")


# --------------------------------------------------------------------------- #
# authenticated — try-on
# --------------------------------------------------------------------------- #
@app.post("/api/tryon")
async def do_tryon(
    garment_id: int = Form(...),
    person: UploadFile | None = File(None),
    photo_id: int | None = Form(None),
    user: dict = Depends(get_current_user),
) -> dict:
    garment = _wardrobe.get(user["id"], garment_id)
    if garment is None:
        raise HTTPException(404, f"garment {garment_id} not found in your wardrobe")
    if photo_id is not None:
        try:
            person_bytes = photos.photo_bytes(user["id"], photo_id)
        except photos.PhotoError as ex:
            raise HTTPException(404, str(ex)) from ex
    elif person is not None:
        person_bytes = await person.read()
    else:
        raise HTTPException(400, "provide a person photo or a saved photo_id")
    if not person_bytes:
        raise HTTPException(400, "empty person image")
    try:
        result = await tryon.run_tryon(person_bytes, garment, user["id"])
    except tryon.ComfyUnavailable as ex:
        raise HTTPException(503, str(ex)) from ex
    out_dir = UPLOAD_DIR / str(user["id"]) / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_name = f"tryon_{garment_id}_{int(time.time())}.png"
    (out_dir / out_name).write_bytes(result)
    return {"result_url": f"/api/uploads/{out_name}", "garment_id": garment_id}


@app.get("/api/uploads/{filename}")
def get_result(filename: str, user: dict = Depends(get_current_user)) -> FileResponse:
    """Serve a try-on result only to the user who owns it (path-traversal safe)."""
    safe = Path(filename).name  # strips any directory components
    path = UPLOAD_DIR / str(user["id"]) / "out" / safe
    if not path.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(path, media_type="image/png")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
