import asyncio
import json
import time
from typing import Optional

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from infra.log import logger
from infra.cache import get_redis
import services.state as state

router = APIRouter()

_SSE_TIMEOUT = 300  # 5 minutes
_POLL_INTERVAL = 1.0  # seconds between inbox polls
_HEARTBEAT_INTERVAL = 15.0  # seconds between SSE heartbeat comments
_CHAT_INBOX_PREFIX = "bridge:chat_inbox:"


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    content: str = ""
    sessionId: Optional[str] = None
    msgType: str = "text"
    media: Optional[dict] = None
    token: Optional[str] = None


class CreateSessionRequest(BaseModel):
    token: Optional[str] = None


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

async def _authenticate(
    authorization: Optional[str],
    body_token: Optional[str],
) -> Optional[str]:
    """Extract and validate token from Authorization header or request body.

    Returns the validated token string, or None if invalid.
    """
    token = None

    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()

    if not token and body_token:
        token = body_token.strip()

    if not token:
        return None

    if await state.token_manager.validate(token):
        return token
    return None


# ---------------------------------------------------------------------------
# Session resolution
# ---------------------------------------------------------------------------

async def _resolve_session(
    token: str,
    session_id: Optional[str],
) -> tuple[str, int]:
    """Resolve or auto-create a session.

    - If session_id is provided, validate it exists.
    - If not provided, restore active session or create new.

    Returns (session_id, session_number).
    Raises ValueError with a message on failure.
    """
    bridge = state.bridge

    if session_id:
        sessions, _ = await bridge.get_sessions(token)
        match = next((s for s in sessions if s[0] == session_id), None)
        if not match:
            raise ValueError(f"Session not found: {session_id}")
        return match[0], match[1]

    # Try to restore active session
    active = await bridge.get_active_session(token)
    if active:
        sessions, _ = await bridge.get_sessions(token)
        match = next((s for s in sessions if s[0] == active), None)
        if match:
            return match[0], match[1]

    # Create new session
    return await bridge.create_session(token)


# ---------------------------------------------------------------------------
# SSE formatting
# ---------------------------------------------------------------------------

def _sse_event(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _sse_comment() -> str:
    return ": heartbeat\n\n"


# ---------------------------------------------------------------------------
# SSE stream generator
# ---------------------------------------------------------------------------

async def _stream_response(
    token: str,
    session_id: str,
    session_number: int,
):
    """Consume events from Redis inbox and yield SSE events."""
    redis = get_redis()
    inbox = f"{_CHAT_INBOX_PREFIX}{token}:{session_id}"
    deadline = time.time() + _SSE_TIMEOUT
    last_heartbeat = time.time()

    # First event: session info
    yield _sse_event("session", {
        "sessionId": session_id,
        "sessionNumber": session_number,
    })

    try:
        while time.time() < deadline:
            raw = await redis.lpop(inbox)

            if raw is None:
                # No message — send heartbeat if interval elapsed, then sleep
                now = time.time()
                if now - last_heartbeat >= _HEARTBEAT_INTERVAL:
                    yield _sse_comment()
                    last_heartbeat = now
                await asyncio.sleep(_POLL_INTERVAL)
                continue

            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("SSE: invalid JSON in inbox (token={}...)", token[:10])
                continue

            event_type = event.pop("type", "message")
            yield _sse_event(event_type, event)

            # Terminal events — close the stream
            if event_type in ("done", "error"):
                return

        # Timeout reached
        yield _sse_event("error", {"content": "Stream timeout"})
    except asyncio.CancelledError:
        # Client disconnected
        logger.info("SSE: client disconnected (token={}...)", token[:10])
    except Exception:
        logger.exception("SSE: stream error (token={}...)", token[:10])
        yield _sse_event("error", {"content": "Internal server error"})


# ---------------------------------------------------------------------------
# POST /bridge/chat — Dialogue endpoint (SSE stream response)
# ---------------------------------------------------------------------------

@router.post("/bridge/chat")
async def chat_sse(
    body: ChatRequest,
    authorization: Optional[str] = Header(default=None),
):
    # Authenticate
    token = await _authenticate(authorization, body.token)
    if not token:
        return JSONResponse(
            status_code=401,
            content={"ok": False, "error": "Invalid or missing token"},
        )

    # Validate message content
    msg_type = body.msgType or "text"
    content = body.content or ""

    if msg_type == "text" and not content:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "Empty message"},
        )

    if msg_type in ("image", "file", "audio", "video") and not body.media:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": f"Missing media info for type: {msg_type}"},
        )

    # Check bot connected
    if not await state.bridge.is_bot_connected(token):
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "No bot connected"},
        )

    # Resolve session
    try:
        session_id, session_number = await _resolve_session(token, body.sessionId)
    except ValueError as e:
        return JSONResponse(
            status_code=404,
            content={"ok": False, "error": str(e)},
        )

    # Ensure this session is the active one (so bot responses route here)
    await state.bridge.switch_session(token, session_id)

    # Send message to bot via Redis inbox
    req_id = await state.bridge.send_to_bot(
        token, content,
        msg_type=msg_type,
        media=body.media,
    )
    if not req_id:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": "Failed to send message to bot"},
        )

    logger.info(
        "SSE: chat started req={} session={} (token={}...)",
        req_id, session_id[:8], token[:10],
    )

    return StreamingResponse(
        _stream_response(token, session_id, session_number),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# GET /bridge/chat/sessions — List sessions
# ---------------------------------------------------------------------------

@router.get("/bridge/chat/sessions")
async def list_sessions(
    authorization: Optional[str] = Header(default=None),
    token: Optional[str] = None,
):
    validated = await _authenticate(authorization, token)
    if not validated:
        return JSONResponse(
            status_code=401,
            content={"ok": False, "error": "Invalid or missing token"},
        )

    sessions, active_id = await state.bridge.get_sessions(validated)

    return {
        "ok": True,
        "sessions": [{"id": s[0], "number": s[1]} for s in sessions],
        "activeSessionId": active_id,
    }


# ---------------------------------------------------------------------------
# POST /bridge/chat/sessions — Create new session
# ---------------------------------------------------------------------------

@router.post("/bridge/chat/sessions")
async def create_session(
    body: CreateSessionRequest = CreateSessionRequest(),
    authorization: Optional[str] = Header(default=None),
):
    validated = await _authenticate(authorization, body.token)
    if not validated:
        return JSONResponse(
            status_code=401,
            content={"ok": False, "error": "Invalid or missing token"},
        )

    session_id, session_number = await state.bridge.create_session(validated)
    sessions, active_id = await state.bridge.get_sessions(validated)

    return {
        "ok": True,
        "sessionId": session_id,
        "sessionNumber": session_number,
        "sessions": [{"id": s[0], "number": s[1]} for s in sessions],
        "activeSessionId": active_id,
    }
