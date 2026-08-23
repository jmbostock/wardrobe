"""Weather + recommendation + stylist chat endpoints."""
from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import db, recommender, stylist, weather
from ..deps import get_current_user
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
    return recommender.recommend(
        w, req.activity, req.prompt, wardrobe=wardrobe, user_id=user["id"],
        owned_only=req.owned_only,
    )


# --------------------------------------------------------------------------- #
# Stylist chat (DeepSeek-v4-flash, SSE streaming)                             #
# --------------------------------------------------------------------------- #

class ChatRequest(BaseModel):
    message: str = Field(..., description="User's chat message to Alta")
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

    garments = wardrobe.all(user_id)
    system_prompt = stylist.build_system_prompt(
        weather=ctx_weather,
        garments=garments,
        outfit=ctx_outfit,
    )

    # Append the new user message to history for the API call
    messages_for_api = history + [{"role": "user", "content": req.message}]

    # --- stream response as SSE ---
    async def event_stream():
        full_reply: list[str] = []
        async for token in stylist.stream_chat(
            system_prompt=system_prompt,
            messages=messages_for_api,
        ):
            if token == "__DONE__":
                # Persist both user message and assistant reply
                _append_messages(conn, lock, session_id, [
                    {"role": "user", "content": req.message},
                    {"role": "assistant", "content": "".join(full_reply)},
                ])
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
