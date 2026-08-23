"""Stylist chat — DeepSeek-v4-flash via OpenAI-compatible API (zero VRAM).

Uses the DeepSeek /chat/completions endpoint with stream=True so the FastAPI
route can pipe tokens straight to the browser as Server-Sent Events.

Graceful degradation: if DEEPSEEK_API_KEY is unset or the call fails, the
caller receives an error token stream rather than raising.
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import httpx

from .config import settings

if TYPE_CHECKING:
    from .wardrobe import Garment

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# System prompt                                                                #
# --------------------------------------------------------------------------- #

_SYSTEM_TEMPLATE = """\
You are Cher, a sharp and friendly personal fashion stylist. \
You know the user's wardrobe inside-out and today's weather.

## USER'S WARDROBE ({garment_count} garments)
{wardrobe_summary}

## TODAY'S WEATHER
{weather_summary}

## CURRENT OUTFIT SUGGESTION
{outfit_summary}

## RULES
- Only suggest garments by their EXACT name as listed in the wardrobe above. Never invent items the user doesn't own.
- When asked counting questions (e.g., "how many pairs of jeans/shirts do I have?"), count ONLY the items in the list above whose name, category, or color matches. Give the exact count based ONLY on the wardrobe list provided. Never invent or hallucinate counts.
- Keep replies concise (2–4 sentences) unless asked for more detail.
- When proposing a swap, name both the item to remove and the exact replacement.
- If asked anything unrelated to fashion or this wardrobe, politely redirect.
- Do not repeat the weather or wardrobe data back verbatim.
"""


def _weather_summary(weather: dict) -> str:
    """Format weather dictionary into a concise single-line text summary."""
    return (
        f"{weather.get('temp_f', '?')}°F "
        f"(feels {weather.get('feels_like_f', '?')}°F), "
        f"{weather.get('condition', 'unknown')}, "
        f"wind {weather.get('wind_kph', 0)} km/h, "
        f"humidity {weather.get('humidity', 0)}%"
    )


def _wardrobe_summary(garments: list[Garment]) -> str:
    """Detailed text list of all garments sorted by rating descending."""
    lines: list[str] = []
    for g in sorted(garments, key=lambda x: (-x.rating, x.name)):
        parts = [f"category: {g.category}"]
        if g.color_tags:
            parts.append(f"color: {g.color_tags}")
        if g.brand:
            parts.append(f"brand: {g.brand}")
        if g.sizes:
            parts.append(f"size: {g.sizes}")
        parts.append(f"formality: {g.formality}")
        parts.append(f"warmth: {g.warmth}/5")
        if g.rating:
            parts.append(f"★{g.rating}/10")
        if g.wear_count:
            parts.append(f"worn {g.wear_count}×")
        lines.append(f"- {g.name} [{', '.join(parts)}]")
    return "\n".join(lines) if lines else "(empty wardrobe)"


def _outfit_summary(outfit: dict) -> str:
    """Format suggested outfit dictionary into a slot-by-slot text summary."""
    parts: list[str] = []
    for slot in ("top", "bottom", "outerwear", "footwear"):
        g = outfit.get(slot)
        if g:
            parts.append(f"{slot}: {g['name']}")
    accs = outfit.get("accessories") or []
    for a in accs:
        parts.append(f"accessory: {a['name']}")
    return ", ".join(parts) if parts else "No outfit suggested yet."


def build_system_prompt(
    *,
    weather: dict,
    garments: list[Garment],
    outfit: dict,
) -> str:
    """Assemble the system prompt for Cher with wardrobe, weather, and active outfit context."""
    return _SYSTEM_TEMPLATE.format(
        weather_summary=_weather_summary(weather),
        garment_count=len(garments),
        wardrobe_summary=_wardrobe_summary(garments),
        outfit_summary=_outfit_summary(outfit),
    )


# --------------------------------------------------------------------------- #
# Streaming call                                                               #
# --------------------------------------------------------------------------- #

async def stream_chat(
    *,
    system_prompt: str,
    messages: list[dict],
    max_tokens: int = 512,
) -> AsyncIterator[str]:
    """Yield text tokens from DeepSeek-v4-flash as they arrive.

    Yields special sentinel strings:
      "__ERROR__:<msg>"  — if the API call fails (no exception raised to caller)
      "__DONE__"         — final sentinel so the caller knows streaming ended

    messages: list of {"role": "user"|"assistant", "content": "..."}
    """
    if not settings.deepseek_api_key:
        yield "__ERROR__:DEEPSEEK_API_KEY is not set — add it to .env"
        yield "__DONE__"
        return

    url = f"{settings.deepseek_base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.deepseek_api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    payload = {
        "model": settings.deepseek_chat_model,
        "stream": True,
        "max_tokens": max_tokens,
        # deepseek-v4-flash is a REASONING model: without this it streams a long
        # `reasoning_content` preamble before any visible text, so the stylist
        # chat looks hung for seconds. Disable thinking for snappy replies.
        "thinking": {"type": "disabled"},
        "messages": [{"role": "system", "content": system_prompt}] + messages,
    }

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, read=60.0)) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    yield f"__ERROR__:DeepSeek {resp.status_code}: {body.decode()[:200]}"
                    yield "__DONE__"
                    return
                async for raw_line in resp.aiter_lines():
                    if not raw_line.startswith("data:"):
                        continue
                    data = raw_line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        token = (
                            chunk.get("choices", [{}])[0]
                            .get("delta", {})
                            .get("content") or ""
                        )
                        if token:
                            yield token
                    except Exception:  # noqa: BLE001
                        continue
    except httpx.TimeoutException:
        yield "__ERROR__:DeepSeek request timed out — check your connection"
    except Exception as exc:  # noqa: BLE001
        log.exception("stylist stream_chat error")
        yield f"__ERROR__:{exc}"

    yield "__DONE__"
