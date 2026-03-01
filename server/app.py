import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

from token_manager import TokenManager
from bridge import ConnectionBridge

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Astron Claw Bridge Server")
token_manager = TokenManager()
bridge = ConnectionBridge()

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


# ── Bot WebSocket ─────────────────────────────────────────────────────────────


@app.websocket("/ws/bot")
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


@app.websocket("/ws/chat")
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

    try:
        while True:
            data = await ws.receive_json()
            msg_type = data.get("type", "")

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
