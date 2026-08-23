"""Stylist chat — DeepSeek-v4-flash via OpenAI-compatible API (zero VRAM).

Uses the DeepSeek /chat/completions endpoint with stream=True so the FastAPI
route can pipe tokens straight to the browser as Server-Sent Events.

Graceful degradation: if DEEPSEEK_API_KEY is unset or the call fails, the
caller receives an error token stream rather than raising.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import httpx

from .config import settings

if TYPE_CHECKING:
    from .wardrobe import Garment

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Outfit marker — Cher flags her MAIN picks so we can render their photos      #
# --------------------------------------------------------------------------- #
_OUTFIT_MARKER_RE = re.compile(r"\[OUTFIT:\s*((?:\"[^\"]*\"\s*,?\s*)*)\]")


def parse_outfit_marker(text: str) -> tuple[list[str], str]:
    """Extract [OUTFIT: "name", "name"] + the reply text with the block removed.

    Cher appends this machine-readable line to flag the pieces she actually
    recommends — alternatives/backups are NOT included. Returns (names, clean).
    """
    if not text:
        return [], text
    m = _OUTFIT_MARKER_RE.search(text)
    if not m:
        return [], text
    names = [n.strip() for n in re.findall(r"\"([^\"]*)\"", m.group(1)) if n.strip()]
    clean = (text[: m.start()] + text[m.end():]).strip()
    return names, clean


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

## YOUR STYLE PROFILE
{style_summary}

## CURRENT OUTFIT SUGGESTION
{outfit_summary}

## RULES
- Only suggest garments by their EXACT name as listed in the wardrobe above. Never invent items the user doesn't own.
- The STYLE PROFILE is your personalization signal: honor its guardrails (e.g., avoid patterns / certain colors / formal wear above their range) and warmth bias when picking, but never quote the profile fields back verbatim.
- "OWNED" = the user owns it; "WISHLIST (NOT OWNED)" = they do NOT own it yet. Only mention wishlist items as possible future buys. When the user asks for something they own, suggest ONLY items labeled OWNED — never claim a wishlist item is owned.
- When asked counting questions (e.g., "how many pairs of jeans/shirts do I have?"), count ONLY the items in the list above whose name, category, or color matches. Give the exact count based ONLY on the wardrobe list provided. Never invent or hallucinate counts.
- Keep replies concise (2–4 sentences) unless asked for more detail.
- When proposing a swap, name both the item to remove and the exact replacement.
- When you recommend a specific look to wear now, APPEND one exact machine line at the very END of your reply: [OUTFIT: "Exact Garment Name", "Exact Garment Name"] — using ONLY exact names from the wardrobe above, listing only the pieces you'd actually wear (no alternatives, no backups), and nothing after it. This line is machine-read and hidden from the user.
- If asked anything unrelated to fashion or this wardrobe, politely redirect.
- Do not repeat the weather or wardrobe data back verbatim.
"""


def _weather_summary(weather: dict) -> str:
    """Format weather dictionary into a concise single-line text summary."""
    loc = weather.get("location")
    return (
        f"{weather.get('temp_f', '?')}°F "
        f"(feels {weather.get('feels_like_f', '?')}°F), "
        f"{weather.get('condition', 'unknown')}, "
        f"wind {weather.get('wind_kph', 0)} km/h, "
        f"humidity {weather.get('humidity', 0)}%"
        + (f" — {loc}" if loc else "")
    )


def _wardrobe_summary(garments: list[Garment]) -> str:
    """Detailed text list of all garments sorted by rating descending."""
    lines: list[str] = []
    for g in sorted(garments, key=lambda x: (-x.rating, x.name)):
        parts = [f"category: {g.category}"]
        parts.append("OWNED" if g.owned else "WISHLIST (NOT OWNED)")
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


def _derived_summary(d: dict) -> str:
    """Human-readable style-profile summary for the system prompt (server-side)."""
    if not d:
        return "(not set yet — keep suggestions neutral)"
    bits: list[str] = []
    if d.get("sex"):
        bits.append(f"sex: {d['sex']}")
    if d.get("height_cm"):
        bits.append(f"height: {d['height_cm']:.0f}cm")
    if d.get("body_build"):
        bits.append(f"build: {d['body_build']}")
    sb = d.get("size_buckets") or {}
    sizes = []
    if sb.get("top"):
        sizes.append(f"top {sb['top']}")
    if sb.get("waist_in"):
        sizes.append(f"waist {sb['waist_in']}\"")
    if sb.get("shoe"):
        sizes.append(f"shoe {sb['shoe']}")
    if sizes:
        bits.append("sizes: " + ", ".join(sizes))
    wb = d.get("warmth_bias")
    if wb:
        bits.append("runs " + ("cold (prefers warmer)" if wb > 0 else "hot (prefers lighter)"))
    fz = d.get("formality_zone") or {}
    if fz.get("min") or fz.get("max"):
        bits.append(f"formality range: {fz.get('min', 'casual')} to {fz.get('max', 'formal')}")
    for g in d.get("guardrails") or []:
        bits.append("avoid " + g.replace("no_", "").replace("avoid_color:", "color ").replace("_", " "))
    if d.get("style_tags"):
        bits.append("style: " + ", ".join(d["style_tags"]))
    ow = d.get("occasion_weights") or {}
    if ow:
        ordered = sorted(ow.items(), key=lambda kv: -kv[1])
        bits.append("typical week: " + ", ".join(f"{k} {v:g}x" for k, v in ordered))
    pal = d.get("palette") or {}
    if pal.get("fav"):
        bits.append("likes colors: " + ", ".join(pal["fav"]))
    if pal.get("avoid"):
        bits.append("avoids colors: " + ", ".join(pal["avoid"]))
    return "; ".join(bits) if bits else "(not set yet — keep suggestions neutral)"


def build_system_prompt(
    *,
    weather: dict,
    garments: list[Garment],
    outfit: dict,
    derived: dict | None = None,
) -> str:
    """Assemble the system prompt for Cher with wardrobe, weather, the active
    outfit, and the user's hidden style profile (personalization signal)."""
    return _SYSTEM_TEMPLATE.format(
        weather_summary=_weather_summary(weather),
        garment_count=len(garments),
        wardrobe_summary=_wardrobe_summary(garments),
        outfit_summary=_outfit_summary(outfit),
        style_summary=_derived_summary(derived or {}),
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
