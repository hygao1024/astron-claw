import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Cookie, Header, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse

from token_manager import TokenManager
from bridge import ConnectionBridge
from admin_auth import AdminAuth
from media_manager import MediaManager, MAX_FILE_SIZE

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Astron Claw Bridge Server")
token_manager = TokenManager()
bridge = ConnectionBridge()
admin_auth = AdminAuth()
media_manager = MediaManager()

# Wire media_manager into bridge so it can resolve download URLs
bridge.set_media_manager(media_manager)

_server_dir = Path(__file__).resolve().parent
# Repo layout: server/ and frontend/ are siblings under project root
# Installed layout: frontend/ is a subdirectory of the server dir
_candidate = _server_dir.parent / "frontend"
frontend_dir = _candidate if _candidate.is_dir() else _server_dir / "frontend"


# ── HTTP API ──────────────────────────────────────────────────────────────────


@app.post("/api/token")
async def create_token():
    token = token_manager.generate()
    return {"token": token}


@app.post("/api/token/validate")
async def validate_token(body: dict):
    token = body.get("token", "")
    valid = token_manager.validate(token)
    return {
        "valid": valid,
        "bot_connected": bridge.is_bot_connected(token) if valid else False,
    }


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_file = frontend_dir / "index.html"
    if index_file.is_file():
        return HTMLResponse(content=index_file.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>Astron Claw</h1><p>Frontend not found.</p>")


@app.get("/admin", response_class=HTMLResponse)
async def serve_admin():
    admin_file = frontend_dir / "admin.html"
    if admin_file.is_file():
        return HTMLResponse(content=admin_file.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>Admin</h1><p>Admin page not found.</p>")


# ── Admin Auth API ────────────────────────────────────────────────────────────


@app.get("/api/admin/auth/status")
async def admin_auth_status(admin_session: str | None = Cookie(default=None)):
    return {
        "need_setup": not admin_auth.is_password_set(),
        "authenticated": admin_auth.validate_session(admin_session),
    }


@app.post("/api/admin/auth/setup")
async def admin_auth_setup(body: dict):
    if admin_auth.is_password_set():
        return JSONResponse({"error": "Password already set"}, status_code=400)
    password = body.get("password", "")
    if len(password) < 4:
        return JSONResponse({"error": "Password too short"}, status_code=400)
    admin_auth.set_password(password)
    session = admin_auth.create_session()
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        key="admin_session", value=session,
        httponly=True, path="/", samesite="lax", max_age=86400,
    )
    return resp


@app.post("/api/admin/auth/login")
async def admin_auth_login(body: dict):
    password = body.get("password", "")
    if not admin_auth.verify_password(password):
        return JSONResponse({"error": "Wrong password"}, status_code=401)
    session = admin_auth.create_session()
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        key="admin_session", value=session,
        httponly=True, path="/", samesite="lax", max_age=86400,
    )
    return resp


@app.post("/api/admin/auth/logout")
async def admin_auth_logout(admin_session: str | None = Cookie(default=None)):
    admin_auth.remove_session(admin_session)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(key="admin_session", path="/")
    return resp


# ── Protected Admin API ──────────────────────────────────────────────────────


def _require_admin(admin_session: str | None):
    if not admin_auth.validate_session(admin_session):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return None


@app.get("/api/admin/tokens")
async def list_tokens(admin_session: str | None = Cookie(default=None)):
    denied = _require_admin(admin_session)
    if denied:
        return denied
    tokens = token_manager.list_all()
    connections = bridge.get_connections_summary()
    result = []
    for t in tokens:
        conn = connections.get(t["token"], {})
        result.append({
            "token": t["token"],
            "name": t.get("name", ""),
            "created_at": t["created_at"],
            "expires_at": t["expires_at"],
            "bot_online": conn.get("bot_online", False),
            "chat_count": conn.get("chat_count", 0),
        })
    return {"tokens": result}


@app.post("/api/admin/tokens")
async def admin_create_token(body: dict = {}, admin_session: str | None = Cookie(default=None)):
    denied = _require_admin(admin_session)
    if denied:
        return denied
    name = body.get("name", "")
    expires_in = body.get("expires_in", 86400)
    token = token_manager.generate(name=name, expires_in=expires_in)
    return {"token": token}


@app.delete("/api/admin/tokens/{token_value}")
async def admin_delete_token(token_value: str, admin_session: str | None = Cookie(default=None)):
    denied = _require_admin(admin_session)
    if denied:
        return denied
    token_manager.remove(token_value)
    return {"ok": True}


@app.patch("/api/admin/tokens/{token_value}")
async def admin_update_token(token_value: str, body: dict, admin_session: str | None = Cookie(default=None)):
    denied = _require_admin(admin_session)
    if denied:
        return denied
    name = body.get("name")
    expires_in = body.get("expires_in")
    if not token_manager.update(token_value, name=name, expires_in=expires_in):
        return JSONResponse({"error": "Token not found"}, status_code=404)
    return {"ok": True}


@app.post("/api/admin/cleanup")
async def admin_cleanup(admin_session: str | None = Cookie(default=None)):
    denied = _require_admin(admin_session)
    if denied:
        return denied
    token_count = token_manager.cleanup_expired()
    media_count = media_manager.cleanup_expired()
    return {"removed_tokens": token_count, "removed_media": media_count}


# ── Media API ────────────────────────────────────────────────────────────────


def _validate_token_header(authorization: str | None) -> str | None:
    """Extract and validate token from Authorization header (Bearer scheme).
    Returns the token string if valid, None otherwise."""
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        token = parts[1]
    else:
        token = authorization
    if token_manager.validate(token):
        return token
    return None


@app.post("/api/media/upload")
async def upload_media(
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
):
    token = _validate_token_header(authorization)
    if not token:
        return JSONResponse({"error": "Invalid or missing token"}, status_code=401)

    # Read file content
    file_data = await file.read()

    if len(file_data) > MAX_FILE_SIZE:
        return JSONResponse(
            {"error": f"File too large. Maximum size is {MAX_FILE_SIZE // (1024*1024)}MB"},
            status_code=413,
        )

    mime_type = file.content_type or "application/octet-stream"
    file_name = file.filename or "unnamed"

    result = media_manager.store(file_data, file_name, mime_type, token)
    if not result:
        return JSONResponse({"error": "Invalid file or unsupported type"}, status_code=400)

    result["downloadUrl"] = f"/api/media/download/{result['mediaId']}"
    return result


@app.get("/api/media/download/{media_id}")
async def download_media(
    media_id: str,
    authorization: str | None = Header(default=None),
    token: str = Query(default=""),
):
    # Accept token from either Authorization header or query param
    auth_token = _validate_token_header(authorization)
    if not auth_token and token:
        auth_token = token if token_manager.validate(token) else None
    if not auth_token:
        return JSONResponse({"error": "Invalid or missing token"}, status_code=401)

    meta = media_manager.get_metadata(media_id)
    if not meta:
        return JSONResponse({"error": "Media not found or expired"}, status_code=404)

    file_path = media_manager.get_file_path(media_id)
    if not file_path:
        return JSONResponse({"error": "Media file missing"}, status_code=404)

    return FileResponse(
        path=str(file_path),
        media_type=meta["mimeType"],
        filename=meta["fileName"],
    )


# ── Bot WebSocket ─────────────────────────────────────────────────────────────


@app.websocket("/bridge/bot")
async def ws_bot(
    ws: WebSocket,
    token: str = Query(default=""),
):
    # Also check header for token
    bot_token = token or (ws.headers.get("x-astron-bot-token", ""))
    if not token_manager.validate(bot_token):
        await ws.accept()
        await ws.close(code=4001, reason="Invalid or missing bot token")
        return

    await ws.accept()

    if not bridge.register_bot(bot_token, ws):
        await ws.send_json({"error": "Another bot is already connected with this token"})
        await ws.close(code=4002, reason="Bot already connected")
        return

    logger.info("Bot connected: %s...", bot_token[:10])
    await bridge.notify_bot_connected(bot_token)
    try:
        while True:
            raw = await ws.receive_text()
            await bridge.handle_bot_message(bot_token, raw)
    except WebSocketDisconnect:
        logger.info("Bot disconnected: %s...", bot_token[:10])
    except Exception:
        logger.exception("Bot connection error: %s...", bot_token[:10])
    finally:
        bridge.unregister_bot(bot_token)
        await bridge.notify_bot_disconnected(bot_token)


# ── Chat WebSocket ────────────────────────────────────────────────────────────


@app.websocket("/bridge/chat")
async def ws_chat(
    ws: WebSocket,
    token: str = Query(default=""),
):
    if not token_manager.validate(token):
        await ws.accept()
        await ws.close(code=4001, reason="Invalid or missing token")
        return

    await ws.accept()
    bridge.register_chat(token, ws)
    logger.info("Chat client connected: %s...", token[:10])

    # Notify chat whether bot is currently online
    await ws.send_json({
        "type": "bot_status",
        "connected": bridge.is_bot_connected(token),
    })

    # Create initial session and send session_info
    session_id, session_number = bridge.create_session(token)
    sessions, active_id = bridge.get_sessions(token)
    await ws.send_json({
        "type": "session_info",
        "sessionId": session_id,
        "sessionNumber": session_number,
        "sessions": [{"id": s[0], "number": s[1]} for s in sessions],
        "activeSessionId": active_id,
    })

    try:
        while True:
            data = await ws.receive_json()
            msg_type = data.get("type", "")

            if msg_type == "new_session":
                new_id, new_num = bridge.create_session(token)
                sessions, active_id = bridge.get_sessions(token)
                await ws.send_json({
                    "type": "new_session_ack",
                    "sessionId": new_id,
                    "sessionNumber": new_num,
                    "sessions": [{"id": s[0], "number": s[1]} for s in sessions],
                    "activeSessionId": active_id,
                })
                continue

            if msg_type == "switch_session":
                target_id = data.get("sessionId", "")
                if bridge.switch_session(token, target_id):
                    sessions, active_id = bridge.get_sessions(token)
                    await ws.send_json({
                        "type": "switch_session_ack",
                        "sessionId": target_id,
                        "sessions": [{"id": s[0], "number": s[1]} for s in sessions],
                        "activeSessionId": active_id,
                    })
                else:
                    await ws.send_json({
                        "type": "error",
                        "content": f"Session {target_id} not found",
                    })
                continue

            if msg_type == "message":
                msg_type_inner = data.get("msgType", "text")
                content = data.get("content", "")
                media = data.get("media")

                # Text messages require content
                if msg_type_inner == "text" and not content:
                    await ws.send_json({"type": "error", "content": "Empty message"})
                    continue

                # Media messages require media info
                if msg_type_inner in ("image", "file", "audio", "video") and not media:
                    await ws.send_json({"type": "error", "content": "Missing media info"})
                    continue

                if not bridge.is_bot_connected(token):
                    await ws.send_json({"type": "error", "content": "No bot connected"})
                    continue

                req_id = await bridge.send_to_bot(
                    token, content,
                    msg_type=msg_type_inner,
                    media=media,
                )
                if not req_id:
                    await ws.send_json({"type": "error", "content": "Failed to send to bot"})

    except WebSocketDisconnect:
        logger.info("Chat client disconnected: %s...", token[:10])
    except Exception:
        logger.exception("Chat connection error: %s...", token[:10])
    finally:
        bridge.unregister_chat(token, ws)


# ── Static assets (CSS, JS, etc.) ──────────────────────────────────────────

if frontend_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")
