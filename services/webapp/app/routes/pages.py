"""Page routes — server-rendered shells for the multi-page UI.

Each route renders a small Jinja template extending `base.html`. The pages are
thin shells: data comes from the JSON API, auth is guarded client-side by
`common.js` (Bearer token in localStorage → redirect to /login on 401).
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter()


def _ctx(request: Request, active: str, page_title: str) -> dict:
    return {"request": request, "active": active, "page_title": page_title}


@router.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/suggest")


@router.get("/login", include_in_schema=False)
def login_page(request: Request):
    return templates.TemplateResponse(
        "auth.html", _ctx(request, "auth", "Log in — Clueless Closet")
    )


@router.get("/suggest", include_in_schema=False)
def suggest_page(request: Request):
    return templates.TemplateResponse(
        "suggest.html", _ctx(request, "suggest", "Suggest — Clueless Closet")
    )


@router.get("/tryon", include_in_schema=False)
def tryon_page(request: Request):
    return templates.TemplateResponse(
        "tryon.html", _ctx(request, "tryon", "Try on — Clueless Closet")
    )


@router.get("/wardrobe", include_in_schema=False)
def wardrobe_page(request: Request):
    return templates.TemplateResponse(
        "wardrobe.html", _ctx(request, "wardrobe", "Wardrobe — Clueless Closet")
    )


@router.get("/outfits", include_in_schema=False)
def outfits_page(request: Request):
    return templates.TemplateResponse(
        "outfits.html", _ctx(request, "outfits", "Outfits — Clueless Closet")
    )


@router.get("/account", include_in_schema=False)
def account_page(request: Request):
    return templates.TemplateResponse(
        "account.html", _ctx(request, "account", "Account — Clueless Closet")
    )
