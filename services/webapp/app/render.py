"""Headless-Chromium fallback for JS-rendered store pages (SPAs like Express).

Many stores serve an empty React/Angular shell to plain HTTP clients — the real
product HTML (JSON-LD, <img> tags) is injected client-side. The webapp's fast
path (httpx + BeautifulSoup) handles normal pages; when that finds no product
images we render the page in headless Chromium via Playwright and hand the final
DOM HTML back to the existing parser.

The browser is launched lazily (first render) and all renders are serialized
behind a lock: FastAPI sync endpoints run on a threadpool, and Playwright's sync
API is not safe for concurrent use. Fails soft — returns None so callers fall
through to their usual "no image" error.
"""
from __future__ import annotations

import logging
import threading

log = logging.getLogger("altacloset.render")

_lock = threading.Lock()
_browser = None
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _browser_or_none():
    """Get (or lazily launch) the shared headless browser. Caller must hold _lock."""
    global _browser
    if _browser is not None:
        return _browser
    try:
        from playwright.sync_api import sync_playwright
    except Exception as ex:  # noqa: BLE001
        log.warning("playwright unavailable (rendering disabled): %s", ex)
        return None
    try:
        _pw = sync_playwright().start()
        _browser = _pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
    except Exception as ex:  # noqa: BLE001
        log.warning("could not launch headless chromium: %s", ex)
        return None
    return _browser


def render_page_html(url: str, timeout: int = 45) -> str | None:
    """Render `url` in headless Chromium and return the final DOM HTML.

    Waits up to ~20s for product JSON-LD to appear (script tags, state="attached"),
    then returns documentElement.outerHTML. Returns None on any failure.
    """
    with _lock:
        browser = _browser_or_none()
        if browser is None:
            return None
        page = None
        try:
            page = browser.new_page(
                user_agent=_UA,
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
            page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            try:
                page.wait_for_selector(
                    'script[type="application/ld+json"]',
                    state="attached",
                    timeout=min(timeout, 20) * 1000,
                )
            except Exception:  # noqa: BLE001 — no JSON-LD; give it a moment anyway
                page.wait_for_timeout(3000)
            page.wait_for_timeout(1500)
            return page.evaluate("() => document.documentElement.outerHTML")
        except Exception as ex:  # noqa: BLE001
            log.warning("render failed for %s: %s", url[:90], ex)
            return None
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:  # noqa: BLE001
                    pass
