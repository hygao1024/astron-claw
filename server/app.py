from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Cookie, Header, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse

from log import logger
from config import load_config
from database import init_db, get_session_factory, close_db
from cache import init_redis, close_redis
from token_manager import TokenManager
from bridge import ConnectionBridge
from admin_auth import AdminAuth
from media_manager import MediaManager, MAX_FILE_SIZE

# These will be initialized during lifespan startup
token_manager: TokenManager
bridge: ConnectionBridge
admin_auth: AdminAuth
media_manager: MediaManager


@asynccontextmanager
async def lifespan(app: FastAPI):
    global token_manager, bridge, admin_auth, media_manager

    config = load_config()

    # Initialize MySQL
    await init_db(config.mysql)
    session_factory = get_session_factory()

    # Initialize Redis
    redis = await init_redis(config.redis)

    # Initialize managers
    token_manager = TokenManager(session_factory)
    admin_auth = AdminAuth(session_factory, redis)
    media_manager = MediaManager(session_factory)
    bridge = ConnectionBridge(redis)
    bridge.set_media_manager(media_manager)
    await bridge.start()

    logger.info("Astron Claw Bridge Server started")
    yield

    # Shutdown — close connections + stop pub/sub before closing infrastructure
    await bridge.shutdown()
    await close_redis()
    await close_db()
    logger.info("Astron Claw Bridge Server stopped")


app = FastAPI(title="Astron Claw Bridge Server", lifespan=lifespan)

_server_dir = Path(__file__).resolve().parent
_candidate = _server_dir.parent / "frontend"
frontend_dir = _candidate if _candidate.is_dir() else _server_dir / "frontend"


# ── Health Check ──────────────────────────────────────────────────────────────


@app.get("/api/health")
async def health_check():
    mysql_ok = False
    redis_ok = False

    try:
        from database import _engine
        if _engine is not None:
            async with _engine.connect() as conn:
                await conn.execute(__import__("sqlalchemy").text("SELECT 1"))
            mysql_ok = True
    except Exception as e:
        logger.warning("MySQL health check failed: {}", str(e))

    try:
        from cache import get_redis
        r = get_redis()
        await r.ping()
        redis_ok = True
    except Exception as e:
        logger.warning("Redis health check failed: {}", str(e))

    status = "ok" if (mysql_ok and redis_ok) else "degraded"
    if status == "degraded":
        logger.warning("Health check degraded — MySQL={}, Redis={}", mysql_ok, redis_ok)
    return {"status": status, "mysql": mysql_ok, "redis": redis_ok}


# ── HTTP API ──────────────────────────────────────────────────────────────────


@app.post("/api/token")
async def create_token():
    token = await token_manager.generate()
    return {"token": token}


@app.post("/api/token/validate")
async def validate_token(body: dict):
    token = body.get("token", "")
    valid = await token_manager.validate(token)
    return {
        "valid": valid,
        "bot_connected": await bridge.is_bot_connected(token) if valid else False,
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
        "need_setup": not await admin_auth.is_password_set(),
        "authenticated": await admin_auth.validate_session(admin_session),
    }


@app.post("/api/admin/auth/setup")
async def admin_auth_setup(body: dict):
    if await admin_auth.is_password_set():
        return JSONResponse({"error": "Password already set"}, status_code=400)
    password = body.get("password", "")
    if len(password) < 4:
        return JSONResponse({"error": "Password too short"}, status_code=400)
    await admin_auth.set_password(password)
    session = await admin_auth.create_session()
    logger.info("Admin password set up for the first time")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        key="admin_session", value=session,
        httponly=True, path="/", samesite="lax", max_age=86400,
    )
    return resp


@app.post("/api/admin/auth/login")
async def admin_auth_login(body: dict):
    password = body.get("password", "")
    if not await admin_auth.verify_password(password):
        logger.warning("Admin login failed — wrong password")
        return JSONResponse({"error": "Wrong password"}, status_code=401)
    session = await admin_auth.create_session()
    logger.info("Admin logged in successfully")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        key="admin_session", value=session,
        httponly=True, path="/", samesite="lax", max_age=86400,
    )
    return resp


@app.post("/api/admin/auth/logout")
async def admin_auth_logout(admin_session: str | None = Cookie(default=None)):
    await admin_auth.remove_session(admin_session)
    logger.info("Admin logged out")
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(key="admin_session", path="/")
    return resp


# ── Protected Admin API ──────────────────────────────────────────────────────


async def _require_admin(admin_session: str | None):
    if not await admin_auth.validate_session(admin_session):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return None


@app.get("/api/admin/tokens")
async def list_tokens(admin_session: str | None = Cookie(default=None)):
    denied = await _require_admin(admin_session)
    if denied:
        return denied
    tokens = await token_manager.list_all()
    connections = await bridge.get_connections_summary()
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
    denied = await _require_admin(admin_session)
    if denied:
        return denied
    name = body.get("name", "")
    expires_in = body.get("expires_in", 86400)
    token = await token_manager.generate(name=name, expires_in=expires_in)
    logger.info("Admin created token: {}... (name={})", token[:16], name)
    return {"token": token}


@app.delete("/api/admin/tokens/{token_value}")
async def admin_delete_token(token_value: str, admin_session: str | None = Cookie(default=None)):
    denied = await _require_admin(admin_session)
    if denied:
        return denied
    await token_manager.remove(token_value)
    await bridge.remove_bot_sessions(token_value)
    logger.info("Admin deleted token: {}...", token_value[:16])
    return {"ok": True}


@app.patch("/api/admin/tokens/{token_value}")
async def admin_update_token(token_value: str, body: dict, admin_session: str | None = Cookie(default=None)):
    denied = await _require_admin(admin_session)
    if denied:
        return denied
    name = body.get("name")
    expires_in = body.get("expires_in")
    if not await token_manager.update(token_value, name=name, expires_in=expires_in):
        return JSONResponse({"error": "Token not found"}, status_code=404)
    logger.info("Admin updated token: {}...", token_value[:16])
    return {"ok": True}


@app.post("/api/admin/cleanup")
async def admin_cleanup(admin_session: str | None = Cookie(default=None)):
    denied = await _require_admin(admin_session)
    if denied:
        return denied
    token_count = await token_manager.cleanup_expired()
    media_count = await media_manager.cleanup_expired()
    logger.info("Admin cleanup: removed {} tokens, {} media files", token_count, media_count)
    return {"removed_tokens": token_count, "removed_media": media_count}


# ── Media API ────────────────────────────────────────────────────────────────


async def _validate_token_header(authorization: str | None) -> str | None:
    """Extract and validate token from Authorization header (Bearer scheme)."""
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        token = parts[1]
    else:
        token = authorization
    if await token_manager.validate(token):
        return token
    return None


@app.post("/api/media/upload")
async def upload_media(
    file: UploadFile = File(...),
    authorization: str | None = Header(default=None),
):
    token = await _validate_token_header(authorization)
    if not token:
        return JSONResponse({"error": "Invalid or missing token"}, status_code=401)

    file_data = await file.read()

    if len(file_data) > MAX_FILE_SIZE:
        logger.warning("Media upload rejected: file too large ({} bytes)", len(file_data))
        return JSONResponse(
            {"error": f"File too large. Maximum size is {MAX_FILE_SIZE // (1024*1024)}MB"},
            status_code=413,
        )

    mime_type = file.content_type or "application/octet-stream"
    file_name = file.filename or "unnamed"

    result = await media_manager.store(file_data, file_name, mime_type, token)
    if not result:
        logger.warning("Media upload rejected: invalid file (name={}, mime={})", file_name, mime_type)
        return JSONResponse({"error": "Invalid file or unsupported type"}, status_code=400)

    result["downloadUrl"] = f"/api/media/download/{result['mediaId']}"
    return result


@app.get("/api/media/download/{media_id}")
async def download_media(
    media_id: str,
    authorization: str | None = Header(default=None),
    token: str = Query(default=""),
):
    auth_token = await _validate_token_header(authorization)
    if not auth_token and token:
        auth_token = token if await token_manager.validate(token) else None
    if not auth_token:
        return JSONResponse({"error": "Invalid or missing token"}, status_code=401)

    meta = await media_manager.get_metadata(media_id)
    if not meta:
        return JSONResponse({"error": "Media not found or expired"}, status_code=404)

    file_path = await media_manager.get_file_path(media_id)
    if not file_path:
        logger.error("Media file missing on disk: {}", media_id)
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
    bot_token = token or (ws.headers.get("x-astron-bot-token", ""))
    if not await token_manager.validate(bot_token):
        await ws.accept()
        await ws.close(code=4001, reason="Invalid or missing bot token")
        logger.warning("Bot connection rejected: invalid token {}...", bot_token[:10])
        return

    await ws.accept()

    if not await bridge.register_bot(bot_token, ws):
        await ws.send_json({"error": "Another bot is already connected with this token"})
        await ws.close(code=4002, reason="Bot already connected")
        logger.warning("Bot connection rejected: duplicate token {}...", bot_token[:10])
        return

    logger.info("Bot connected: {}...", bot_token[:10])
    await bridge.notify_bot_connected(bot_token)
    try:
        while True:
            raw = await ws.receive_text()
            await bridge.handle_bot_message(bot_token, raw)
    except WebSocketDisconnect:
        logger.info("Bot disconnected: {}...", bot_token[:10])
    except Exception:
        logger.exception("Bot connection error: {}...", bot_token[:10])
    finally:
        await bridge.unregister_bot(bot_token)
        await bridge.notify_bot_disconnected(bot_token)


# ── Chat WebSocket ────────────────────────────────────────────────────────────


@app.websocket("/bridge/chat")
async def ws_chat(
    ws: WebSocket,
    token: str = Query(default=""),
):
    if not await token_manager.validate(token):
        await ws.accept()
        await ws.close(code=4001, reason="Invalid or missing token")
        logger.warning("Chat connection rejected: invalid token {}...", token[:10])
        return

    await ws.accept()
    await bridge.register_chat(token, ws)
    logger.info("Chat client connected: {}...", token[:10])

    await ws.send_json({
        "type": "bot_status",
        "connected": await bridge.is_bot_connected(token),
    })

    # Restore active session if one exists in Redis; otherwise create new
    existing_session = await bridge.get_active_session(token)
    if existing_session:
        sessions, active_id = await bridge.get_sessions(token)
        session_id = existing_session
        session_number = next((s[1] for s in sessions if s[0] == existing_session), 1)
        logger.info("Chat session restored: {} (token={}...)", session_id[:8], token[:10])
    else:
        session_id, session_number = await bridge.create_session(token)
        sessions, active_id = await bridge.get_sessions(token)
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
                new_id, new_num = await bridge.create_session(token)
                sessions, active_id = await bridge.get_sessions(token)
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
                if await bridge.switch_session(token, target_id):
                    sessions, active_id = await bridge.get_sessions(token)
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

                if msg_type_inner == "text" and not content:
                    await ws.send_json({"type": "error", "content": "Empty message"})
                    continue

                if msg_type_inner in ("image", "file", "audio", "video") and not media:
                    await ws.send_json({"type": "error", "content": "Missing media info"})
                    continue

                if not await bridge.is_bot_connected(token):
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
        logger.info("Chat client disconnected: {}...", token[:10])
    except Exception:
        logger.exception("Chat connection error: {}...", token[:10])
    finally:
        await bridge.unregister_chat(token, ws)


# ── Static assets (CSS, JS, etc.) ──────────────────────────────────────────

if frontend_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")
