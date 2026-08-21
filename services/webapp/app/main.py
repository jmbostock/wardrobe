"""altacloset — FastAPI entrypoint (multi-user).

Routes:
  GET  /                      → web UI (public)
  GET  /health                → liveness
  POST /api/auth/register     → {username,password} → {token, user}
  POST /api/auth/login        → {username,password} → {token, user}
  POST /api/auth/logout       → revoke session
  GET  /api/auth/me           → current user
  -- everything below requires `Authorization: Bearer <token>` --
  GET  /api/weather           → current weather
  GET  /api/wardrobe          → caller's garments
  POST /api/recommend         → outfit for caller's wardrobe
  POST /api/tryon             → add a garment to a person photo (CatVTON)
  GET  /api/uploads/{name}    → caller's try-on result image (private)
"""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, recommender, tryon, weather
from .config import settings
from .recommender import Weather
from .wardrobe import Wardrobe

app = FastAPI(title="altacloset", version="0.2.0")

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
    username: str = Field(..., min_length=1, max_length=40)
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


# --------------------------------------------------------------------------- #
# public
# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> dict:
    return {"ok": True, "service": "altacloset", "version": "0.2.0"}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/auth/register")
def register(req: AuthRequest) -> dict:
    try:
        user = auth.create_user(req.username, req.password)
    except auth.AuthError as ex:
        raise HTTPException(400, str(ex)) from ex
    token = auth.create_session(user["id"])
    return {"token": token, "user": user}


@app.post("/api/auth/login")
def login(req: AuthRequest) -> dict:
    user = auth.authenticate(req.username, req.password)
    if user is None:
        raise HTTPException(401, "invalid username or password")
    token = auth.create_session(user["id"])
    return {"token": token, "user": user}


# --------------------------------------------------------------------------- #
# authenticated
# --------------------------------------------------------------------------- #
@app.post("/api/auth/logout")
def logout(user: dict = Depends(get_current_user), authorization: str = Header(default="")) -> dict:
    auth.delete_session(authorization[7:])
    return {"ok": True}


@app.get("/api/auth/me")
def me(user: dict = Depends(get_current_user)) -> dict:
    return {"user": user}


@app.get("/api/weather")
def get_weather(_: dict = Depends(get_current_user)) -> dict:
    try:
        return weather.fetch().__dict__
    except Exception as ex:  # noqa: BLE001 — surface fetch failures clearly
        raise HTTPException(502, f"weather fetch failed: {ex}") from ex


@app.get("/api/wardrobe")
def list_wardrobe(user: dict = Depends(get_current_user)) -> list[dict]:
    _wardrobe.seed_for_user(user["id"])
    return [g.to_dict() for g in _wardrobe.all(user["id"])]


@app.post("/api/recommend")
def recommend_outfit(req: RecommendRequest, user: dict = Depends(get_current_user)) -> dict:
    w = Weather(**req.weather.__dict__) if req.weather else weather.fetch()
    return recommender.recommend(w, req.activity, req.prompt, wardrobe=_wardrobe, user_id=user["id"])


@app.post("/api/tryon")
async def do_tryon(
    garment_id: int = Form(...),
    person: UploadFile = File(...),
    user: dict = Depends(get_current_user),
) -> dict:
    garment = _wardrobe.get(user["id"], garment_id)
    if garment is None:
        raise HTTPException(404, f"garment {garment_id} not found in your wardrobe")
    person_bytes = await person.read()
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
