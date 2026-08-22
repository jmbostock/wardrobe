"""Clueless Closet — FastAPI entrypoint (multi-page).

Pages (public shells, client-guarded):
  GET /login · GET /suggest · GET /tryon · GET /wardrobe · GET /outfits ·
  GET /account · GET / (→ /suggest)
Health:
  GET /health
API (Bearer token; see the per-domain routers under app/routes/):
  auth:     POST /api/auth/register|login|logout · GET /api/auth/me
  account:  GET /api/account · POST /api/account/password|location
  photos:   GET/POST /api/photos · PATCH/DELETE /api/photos/{id} ·
            POST /api/photos/{id}/default · GET /api/photos/{id}/image
  wardrobe: GET/POST /api/wardrobe · PATCH/DELETE /api/wardrobe/{id} ·
            POST /api/wardrobe/parse-link · POST /api/wardrobe/{id}/image(-url) ·
            GET /api/wardrobe/{id}/image
  outfits:  GET/POST /api/outfits · PATCH/DELETE /api/outfits/{id}
  tryon:    POST /api/tryon(,outfit) · GET /api/uploads/{name}
  recommend:GET /api/weather · POST /api/recommend · POST /api/image-quality
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import routes
from .version import __version__

app = FastAPI(title="altacloset", version=__version__)

STATIC_DIR = Path(__file__).parent / "static"


@app.get("/health")
def health() -> dict:
    return {"ok": True, "service": "clueless-closet", "version": __version__}


app.include_router(routes.pages.router)
app.include_router(routes.auth_routes.router)
app.include_router(routes.account_routes.router)
app.include_router(routes.photos_routes.router)
app.include_router(routes.wardrobe_routes.router)
app.include_router(routes.outfits_routes.router)
app.include_router(routes.tryon_routes.router)
app.include_router(routes.recommend_routes.router)
app.include_router(routes.image_routes.router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
