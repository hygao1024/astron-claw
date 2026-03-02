import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Cookie
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse

from token_manager import TokenManager
from bridge import ConnectionBridge
from admin_auth import AdminAuth

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Astron Claw Bridge Server")
token_manager = TokenManager()
bridge = ConnectionBridge()
admin_auth = AdminAuth()

frontend_dir = Path(__file__).resolve().parent.parent / "frontend"


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
    count = token_manager.cleanup_expired()
    return {"removed": count}


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
                content = data.get("content", "")
                if not content:
                    await ws.send_json({"type": "error", "content": "Empty message"})
                    continue

                if not bridge.is_bot_connected(token):
                    await ws.send_json({"type": "error", "content": "No bot connected"})
                    continue

                req_id = await bridge.send_to_bot(token, content)
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
