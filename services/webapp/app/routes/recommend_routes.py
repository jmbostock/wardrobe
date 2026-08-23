"""Weather + recommendation + stylist chat endpoints."""
from __future__ import annotations

import json
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import db, interactions, recommender, stylist, weather
from ..deps import get_current_user
from ..media import garment_image_path
from ..recommender import Weather
from ..store import wardrobe

router = APIRouter()


# --------------------------------------------------------------------------- #
# Weather                                                                      #
# --------------------------------------------------------------------------- #

class WeatherIn(BaseModel):
    temp_c: float
    feels_like_c: float | None = None
    condition: str = "clear"
    wind_kph: float = 0.0
    humidity: int = 50
    uv_index: float = 0.0


def _user_coords(user: dict) -> tuple[float, float]:
    lat = user["lat"] if user["lat"] is not None else weather.DEFAULT_LOCATION["lat"]
    lon = user["lon"] if user["lon"] is not None else weather.DEFAULT_LOCATION["lon"]
    return float(lat), float(lon)


@router.get("/api/weather")
def get_weather(user: dict = Depends(get_current_user)) -> dict:
    lat, lon = _user_coords(user)
    try:
        return weather.fetch(lat, lon).to_dict()
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(502, f"weather fetch failed: {ex}") from ex


# --------------------------------------------------------------------------- #
# Rule-based recommend                                                         #
# --------------------------------------------------------------------------- #

class RecommendRequest(BaseModel):
    activity: str = Field("casual", description="office, date, hiking, ...")
    prompt: str | None = Field(None, description="free-form style hint")
    owned_only: bool = Field(False, description="only recommend garments you own")
    weather: WeatherIn | None = Field(None, description="omit to auto-fetch")


@router.post("/api/recommend")
def recommend_outfit(req: RecommendRequest, user: dict = Depends(get_current_user)) -> dict:
    if req.weather:
        w = Weather(**req.weather.__dict__)
    else:
        lat, lon = _user_coords(user)
        w = weather.fetch(lat, lon)
    result = recommender.recommend(
        w, req.activity, req.prompt, wardrobe=wardrobe, user_id=user["id"],
        owned_only=req.owned_only,
    )
    # attach has_image to each recommended garment so the Suggest page can show
    # the top/bottom/etc photo (not just a color swatch)
    result["outfit"] = _with_image_flags(user["id"], result["outfit"])
    return result


class RecommendFeedbackIn(BaseModel):
    outfit: dict = {}
    kind: str = "liked"           # liked | disliked
    activity: str = "casual"
    prompt: str | None = None


@router.post("/api/recommend/feedback")
def recommend_feedback(req: RecommendFeedbackIn, user: dict = Depends(get_current_user)) -> dict:
    """Thumbs-up/down from the chat recommendation bubble → logs liked/disliked
    for every garment in the outfit (with the activity context) so the engine
    learns what works for each occasion."""
    if req.kind not in ("liked", "disliked"):
        raise HTTPException(400, "kind must be 'liked' or 'disliked'")
    ids: list[int] = []
    for slot in ("top", "bottom", "outerwear", "footwear"):
        g = req.outfit.get(slot)
        if g and g.get("id"):
            ids.append(int(g["id"]))
    for g in req.outfit.get("accessories") or []:
        if g and g.get("id"):
            ids.append(int(g["id"]))
    if ids:
        interactions.log_many(user["id"], ids, req.kind,
                              {"source": "chat", "activity": req.activity,
                               "prompt": req.prompt or ""})
    return {"ok": True, "logged": len(ids), "kind": req.kind}


def _with_image_flags(user_id: int, outfit: dict) -> dict:
    """Attach boolean `has_image` to every garment in the recommended outfit.

    Checks on-disk photo existence via `garment_image_path` so the frontend
    Suggest tab can display actual garment photo thumbnails instead of generic
    color swatches.
    """
    def flag(g):
        if isinstance(g, dict) and g.get("id"):
            g["has_image"] = garment_image_path(user_id, g["id"]) is not None
        return g
    for key, val in outfit.items():
        if isinstance(val, list):
            outfit[key] = [flag(g) for g in val]
        else:
            outfit[key] = flag(val)
    return outfit


# --------------------------------------------------------------------------- #
# Consolidated suggest → chat (recommendation posts into the stylist thread)   #
# --------------------------------------------------------------------------- #

def _location_label(user: dict) -> str:
    """Human-readable location name for Cher's recommendation prose."""
    if user.get("location"):
        return user["location"]
    if user.get("lat") is None:
        return weather.DEFAULT_LOCATION["name"]
    return f"{user['lat']:.4f}, {user['lon']:.4f}"


_ZIP_RE = re.compile(r"\b(\d{5})\b")
# only trigger on an explicit travel/weather/wear question so "in the office"
# or "going to work" never get geocoded by accident
_PLACE_RE = re.compile(
    r"\b(?:weather|wear|going|heading|visiting|flying|driving|travel(?:ing|ling)?|trip)"
    r"\s+(?:in|to|at|for)\s+([A-Za-z][A-Za-z .'-]{1,40})",
    re.I,
)
_LOCATION_STOPWORDS = {"work", "home", "office", "gym", "school", "bed",
                       "beach", "party", "store", "mall", "the", "a", "an"}


def _detect_location(message: str, user: dict) -> dict | None:
    """If the user asks about a DIFFERENT city/zip, resolve it and fetch its
    weather so the recommendation is built for where they're GOING, not where
    they are. Returns {name, lat, lon, weather} or None."""
    m = _ZIP_RE.search(message)
    query = m.group(1) if m else None
    if query is None:
        pm = _PLACE_RE.search(message)
        if not pm:
            return None
        query = pm.group(1).strip()
        if len(query) < 3 or query.lower() in _LOCATION_STOPWORDS:
            return None
    loc = weather.geocode(query)
    if not loc or not loc.get("name"):
        return None
    if user.get("lat") is not None and user.get("lon") is not None:
        try:  # same place they're in → fall back to normal behavior
            if abs(loc["lat"] - user["lat"]) < 0.02 and abs(loc["lon"] - user["lon"]) < 0.02:
                return None
        except Exception:  # noqa: BLE001
            pass
    try:
        w = weather.fetch(loc["lat"], loc["lon"])
    except Exception:  # noqa: BLE001
        return None
    return {"name": loc.get("name") or query, "lat": loc["lat"], "lon": loc["lon"], "weather": w}


_ACTIVITY_LABELS = {
    "office": "work", "work": "work", "interview": "interview",
    "date": "date", "dinner": "dinner", "night": "night out",
    "casual": "casual day", "errands": "errands", "home": "day at home",
    "hiking": "hike", "gym": "workout", "beach": "beach day",
    "wedding": "wedding", "gala": "gala", "formal": "formal event",
}


_FORMALITY_LABELS = {
    "casual": "casual",
    "smart-casual": "smart-casual",
    "business": "business-appropriate",
    "formal": "formal",
}


def _join_clauses(clauses: list[str]) -> str:
    """Join a list of phrases into one natural comma/and sentence."""
    if not clauses:
        return ""
    if len(clauses) == 1:
        return clauses[0]
    if len(clauses) == 2:
        return f"{clauses[0]} and {clauses[1]}"
    return f"{', '.join(clauses[:-1])}, and {clauses[-1]}"


def _recommend_intro(activity: str, weather_used: dict, outfit: dict, prompt: str | None,
                     location: str | None = None) -> str:
    """Cher's recommendation message — ONE natural chat bubble, WHY woven in.

    Reads like the stylist actually recommended it: the pieces up front, then the
    reasoning (weather/location, dress code, color harmony, layers, prompt) as
    flowing prose — no separate reasons card duplicating the same info. The user
    can then just keep talking to Cher about the picks. `location` (when known)
    is folded into the lead: "Based on San Mateo weather of 87°F …".
    """
    slot_names: list[str] = []
    for slot in ("top", "bottom", "outerwear", "footwear"):
        g = outfit.get(slot)
        if g:
            slot_names.append(g["name"])
    for a in outfit.get("accessories") or []:
        slot_names.append(a["name"])

    label = _ACTIVITY_LABELS.get(activity, activity)

    if not slot_names:
        return (
            f"I couldn't put together a full {label} look right now. "
            "Add a few more pieces in the Wardrobe tab and hit Suggest again — "
            "or tell me what vibe you're going for."
        )

    # --- why, as natural prose (NO garment names — they're pictured right below) ---
    clauses: list[str] = []
    temp = weather_used.get("temp_f")
    cond = weather_used.get("condition", "clear")
    # (location + temp go in the lead line below, not the trailing why-clauses)
    formality = recommender.ACTIVITY_MAP.get(
        activity.lower(), recommender.ACTIVITY_MAP["casual"]
    )[0]
    clauses.append(f"I matched a {_FORMALITY_LABELS.get(formality, formality)} dress code")
    top = outfit.get("top")
    bottom = outfit.get("bottom")
    if top and top.get("category") in ("dress", "swimsuit"):
        clauses.append("it's a one-piece, so no separate bottom is needed")
    elif top and bottom:
        clauses.append("the pieces are color-compatible")
    if outfit.get("outerwear"):
        clauses.append("I added a layer for warmth")
    if prompt:
        clauses.append(f"I kept your “{prompt}” hint in mind")

    why = _join_clauses(clauses)
    why = why[:1].upper() + why[1:] + "."  # uppercase 1st char only (keep 71°F etc.)
    if location and temp is not None:
        lead = (
            f"Based on {location} weather of {temp:.0f}°F ({cond}), here's your "
            f"{label} look — {', '.join(slot_names)}."
        )
    else:
        lead = f"Here's your {label} look — {', '.join(slot_names)}."
    intro = f"{lead} {why} Ask me to swap anything — color, layers, or vibe — and I'll adjust."
    # Flag any wishlist picks so the user knows what's owned vs aspirational
    unowned: list[str] = [
        g["name"]
        for slot in ("top", "bottom", "outerwear", "footwear")
        if (g := outfit.get(slot)) and not g.get("owned")
    ]
    for a in outfit.get("accessories") or []:
        if not a.get("owned"):
            unowned.append(a["name"])
    if unowned:
        unowned = list(dict.fromkeys(unowned))  # de-dupe
        plural = len(unowned) > 1
        intro += (
            f" Heads up: {', '.join(unowned)} {'are' if plural else 'is'} "
            "on your wishlist, not owned yet."
        )
    return intro


class SuggestRequest(BaseModel):
    session_id: str | None = Field(None, description="Existing chat session to append to; None = new session")
    activity: str = Field("casual", description="office, date, hiking, ...")
    prompt: str | None = Field(None, description="free-form style hint")
    # Default to OWNED-only: suggesting things you don't own yet is confusing.
    # Unchecking "Owned only" in the UI sends owned_only=false to include wishlist.
    owned_only: bool = Field(True, description="only recommend garments you own")
    weather: WeatherIn | None = Field(None, description="omit to auto-fetch")


def _update_context(conn, lock, session_id: str, context: dict) -> None:
    """Replace the stored snapshot of {weather, outfit, activity} for a session."""
    with lock:
        conn.execute(
            "UPDATE chat_sessions SET context=?, updated_at=datetime('now') WHERE id=?",
            (json.dumps(context), session_id),
        )
        conn.commit()


@router.post("/api/suggest")
def suggest_outfit(req: SuggestRequest, user: dict = Depends(get_current_user)) -> dict:
    """Run the rule-based recommendation and post it INTO the stylist chat.

    The recommendation is persisted as a `recommend`-kind assistant message so
    it renders as a rich card in the chat thread (garment photos + reasoning),
    and the conversation can continue from there with Cher.
    """
    conn, lock = _get_conn()
    user_id = user["id"]

    # 1. weather + recommendation (identical picks to /api/recommend)
    if req.weather:
        w = Weather(**req.weather.__dict__)
    else:
        lat, lon = _user_coords(user)
        w = weather.fetch(lat, lon)
    result = recommender.recommend(
        w, req.activity, req.prompt, wardrobe=wardrobe, user_id=user_id,
        owned_only=req.owned_only,
    )
    result["outfit"] = _with_image_flags(user_id, result["outfit"])

    # 2. resolve the chat session (append when valid, else start a new one)
    session_id = req.session_id
    if session_id and not _load_session(conn, lock, session_id, user_id):
        session_id = None
    if not session_id:
        session_id = _create_session(conn, lock, user_id, {"weather": {}, "outfit": {}, "activity": req.activity})

    # 3. Cher's intro + persist the recommendation message + refresh context
    intro = _recommend_intro(req.activity, result["weather_used"], result["outfit"],
                             req.prompt, location=_location_label(user))
    rec_msg = {
        "role": "assistant",
        "kind": "recommend",
        "content": intro,
        "data": {
            "activity": req.activity,
            "prompt": req.prompt,
            "outfit": result["outfit"],
            "reasoning": result["reasoning"],
            "weather_used": result["weather_used"],
        },
    }
    _append_messages(conn, lock, session_id, [rec_msg])
    _update_context(conn, lock, session_id, {
        "weather": result["weather_used"],
        "outfit": result["outfit"],
        "activity": req.activity,
    })

    session = _load_session(conn, lock, session_id, user_id)
    return {
        "session_id": session_id,
        "intro": intro,
        "outfit": result["outfit"],
        "reasoning": result["reasoning"],
        "weather_used": result["weather_used"],
        "activity": req.activity,
        "prompt": req.prompt,
        "messages": json.loads(session["messages"]),
    }


# --------------------------------------------------------------------------- #
# Stylist chat (DeepSeek-v4-flash, SSE streaming)                             #
# --------------------------------------------------------------------------- #

class ChatRequest(BaseModel):
    message: str = Field(..., description="User's chat message to Cher")
    session_id: str | None = Field(None, description="Existing session UUID; None = new session")
    # Context for bootstrapping a new session (passed by the frontend after /api/recommend)
    activity: str = Field("casual")
    weather_ctx: WeatherIn | None = Field(None, description="Weather used for current recommendation")
    outfit_ctx: dict | None = Field(None, description="Outfit dict from /api/recommend")


def _get_conn():
    return db.init(), db.lock()


def _load_session(conn, lock, session_id: str, user_id: int) -> dict | None:
    with lock:
        row = conn.execute(
            "SELECT * FROM chat_sessions WHERE id=? AND user_id=?",
            (session_id, user_id),
        ).fetchone()
    return dict(row) if row else None


def _create_session(conn, lock, user_id: int, context: dict) -> str:
    sid = str(uuid.uuid4())
    with lock:
        conn.execute(
            "INSERT INTO chat_sessions (id, user_id, messages, context) VALUES (?,?,?,?)",
            (sid, user_id, "[]", json.dumps(context)),
        )
        conn.commit()
    return sid


def _append_messages(conn, lock, session_id: str, new_messages: list[dict]) -> None:
    """Load existing messages, append new ones, write back."""
    with lock:
        row = conn.execute(
            "SELECT messages FROM chat_sessions WHERE id=?", (session_id,)
        ).fetchone()
        if not row:
            return
        msgs = json.loads(row["messages"])
        msgs.extend(new_messages)
        conn.execute(
            "UPDATE chat_sessions SET messages=?, updated_at=datetime('now') WHERE id=?",
            (json.dumps(msgs), session_id),
        )
        conn.commit()


def _resolve_outfit_items(user_id: int, names: list[str]) -> list[dict]:
    """Resolve [OUTFIT: ...] garment names to {id, name, category} for the chat
    photo cards (category lets the Try-on page slot them into the look)."""
    if not names:
        return []
    by_name = {}
    for g in wardrobe.all(user_id):
        by_name.setdefault(g.name, g)
    seen = set()
    items = []
    for n in names:
        g = by_name.get(n)
        if g and g.id not in seen:
            seen.add(g.id)
            items.append({"id": g.id, "name": g.name, "category": g.category})
    return items[:6]


@router.post("/api/recommend/chat")
async def stylist_chat(
    req: ChatRequest,
    user: dict = Depends(get_current_user),
) -> StreamingResponse:
    conn, lock = _get_conn()
    user_id = user["id"]

    # --- resolve / create session ---
    session_id = req.session_id
    if session_id:
        session = _load_session(conn, lock, session_id, user_id)
        if not session:
            # Stale / wrong user — start fresh
            session_id = None

    if not session_id:
        # Snapshot the current weather + outfit as context for this session
        if req.weather_ctx:
            w = Weather(**req.weather_ctx.__dict__)
        else:
            lat, lon = _user_coords(user)
            try:
                w = weather.fetch(lat, lon)
            except Exception:  # noqa: BLE001
                w = Weather(temp_c=20.0)

        ctx_weather = w.to_dict()
        ctx_outfit = req.outfit_ctx or {}
        context = {"weather": ctx_weather, "outfit": ctx_outfit, "activity": req.activity}
        session_id = _create_session(conn, lock, user_id, context)
        session = {"messages": "[]", "context": json.dumps(context)}

    # --- build prompt inputs ---
    context = json.loads(session["context"])
    ctx_weather = context.get("weather", {})
    ctx_outfit = context.get("outfit", {})
    history: list[dict] = json.loads(session["messages"])

    # If the user asks about a DIFFERENT city/zip, build the recommendation for
    # where they're GOING: fetch that weather and post a look card into the chat.
    location_recommend = None
    target = _detect_location(req.message, user)
    prompt_weather = ctx_weather
    prompt_outfit = ctx_outfit
    if target:
        prompt_weather = target["weather"].to_dict()
        prompt_weather["location"] = target["name"]
        result = recommender.recommend(
            target["weather"], req.activity, None, wardrobe=wardrobe,
            user_id=user_id, owned_only=True,
        )
        result["outfit"] = _with_image_flags(user_id, result["outfit"])
        intro = _recommend_intro(req.activity, result["weather_used"], result["outfit"],
                                 None, location=target["name"])
        _append_messages(conn, lock, session_id, [{
            "role": "assistant", "kind": "recommend", "content": intro,
            "data": {"activity": req.activity, "prompt": None,
                      "outfit": result["outfit"], "reasoning": result["reasoning"],
                      "weather_used": result["weather_used"]},
        }])
        _update_context(conn, lock, session_id, {
            "weather": result["weather_used"], "outfit": result["outfit"],
            "activity": req.activity,
        })
        prompt_outfit = result["outfit"]
        location_recommend = {"intro": intro, "outfit": result["outfit"],
                              "activity": req.activity}

    garments = wardrobe.all(user_id)
    system_prompt = stylist.build_system_prompt(
        weather=prompt_weather,
        garments=garments,
        outfit=prompt_outfit,
        derived=user.get("derived_profile"),
    )

    # Append the new user message to history for the API call
    messages_for_api = history + [{"role": "user", "content": req.message}]

    # --- stream response as SSE ---
    async def event_stream():
        if location_recommend:
            yield f"data: {json.dumps({'type': 'recommend', 'intro': location_recommend['intro'], 'outfit': location_recommend['outfit'], 'activity': location_recommend['activity']})}\n\n"
        full_reply: list[str] = []
        async for token in stylist.stream_chat(
            system_prompt=system_prompt,
            messages=messages_for_api,
        ):
            if token == "__DONE__":
                # Parse Cher's [OUTFIT: ...] marker → resolve to real garment
                # photos so the chat can render her main picks with images.
                full = "".join(full_reply)
                names, clean = stylist.parse_outfit_marker(full)
                items = _resolve_outfit_items(user_id, names)
                _append_messages(conn, lock, session_id, [
                    {"role": "user", "content": req.message},
                    {"role": "assistant", "content": clean, "garments": items},
                ])
                yield f"data: {json.dumps({'type': 'garments', 'items': items})}\n\n"
                yield f"data: {json.dumps({'type': 'done', 'session_id': session_id})}\n\n"
                return
            if token.startswith("__ERROR__:"):
                err = token[len("__ERROR__:"):]
                yield f"data: {json.dumps({'type': 'error', 'message': err})}\n\n"
                yield f"data: {json.dumps({'type': 'done', 'session_id': session_id})}\n\n"
                return
            full_reply.append(token)
            yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering if behind a proxy
        },
    )


@router.get("/api/recommend/chat/{session_id}")
def get_chat_history(
    session_id: str,
    user: dict = Depends(get_current_user),
) -> dict:
    conn, lock = _get_conn()
    session = _load_session(conn, lock, session_id, user["id"])
    if not session:
        raise HTTPException(404, "session not found")
    return {
        "session_id": session_id,
        "messages": json.loads(session["messages"]),
        "context": json.loads(session["context"]),
    }


@router.delete("/api/recommend/chat/{session_id}")
def delete_chat_session(
    session_id: str,
    user: dict = Depends(get_current_user),
) -> dict:
    conn, lock = _get_conn()
    with lock:
        cur = conn.execute(
            "DELETE FROM chat_sessions WHERE id=? AND user_id=?",
            (session_id, user["id"]),
        )
        conn.commit()
    if not cur.rowcount:
        raise HTTPException(404, "session not found")
    return {"deleted": session_id}
