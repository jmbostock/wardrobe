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
  tryon:    POST /api/tryon(,outfit) · POST /api/tryon/clip · GET /api/clips/{id} ·
            GET /api/uploads/{name}
  recommend:GET /api/weather · POST /api/recommend · POST /api/image-quality
  admin (dev-only, login = `admin` user): GET /api/admin/users ·
                    POST /api/admin/impersonate (act as user) ·
                    POST /api/admin/test-copy · GET /api/admin/test
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import auth, routes
from .version import __version__

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # create the dev `admin`/`test` accounts when DEV_ADMIN_ENABLED=1
    auth.ensure_dev_accounts()
    yield


app = FastAPI(title="altacloset", version=__version__, lifespan=lifespan)


class NoCacheStaticFiles(StaticFiles):
    """Serve static assets with `Cache-Control: no-cache` so CSS/JS updates reach
    phones immediately. Without a cache header browsers heuristic-cache
    app.css/suggest.js for a long time and users keep seeing the old UI."""

    def file_response(self, full_path, stat_result, scope, status_code=200):
        resp = super().file_response(full_path, stat_result, scope, status_code)
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp


@app.get("/health")
def health() -> dict:
    return {"ok": True, "service": "clueless-closet", "version": __version__}


app.include_router(routes.pages.router)
app.include_router(routes.auth_routes.router)
app.include_router(routes.admin_routes.router)
app.include_router(routes.test_routes.router)
app.include_router(routes.account_routes.router)
app.include_router(routes.photos_routes.router)
app.include_router(routes.wardrobe_routes.router)
app.include_router(routes.outfits_routes.router)
app.include_router(routes.tryon_routes.router)
app.include_router(routes.recommend_routes.router)
app.include_router(routes.image_routes.router)
app.mount("/static", NoCacheStaticFiles(directory=STATIC_DIR), name="static")
