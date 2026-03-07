from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query

from infra.log import logger
import services.state as state

router = APIRouter()


@router.websocket("/bridge/bot")
async def ws_bot(
    ws: WebSocket,
    token: str = Query(default=""),
):
    bot_token = token or (ws.headers.get("x-astron-bot-token", ""))
    if not await state.token_manager.validate(bot_token):
        await ws.accept()
        await ws.close(code=4001, reason="Invalid or missing bot token")
        logger.warning("Bot connection rejected: invalid token {}...", bot_token[:10])
        return

    await ws.accept()

    if not await state.bridge.register_bot(bot_token, ws):
        await ws.send_json({"error": "Another bot is already connected with this token"})
        await ws.close(code=4002, reason="Bot already connected")
        logger.warning("Bot connection rejected: duplicate token {}...", bot_token[:10])
        return

    logger.info("Bot connected: {}...", bot_token[:10])
    await state.bridge.notify_bot_connected(bot_token)
    try:
        while True:
            raw = await ws.receive_text()
            await state.bridge.handle_bot_message(bot_token, raw)
    except WebSocketDisconnect:
        logger.info("Bot disconnected: {}...", bot_token[:10])
    except Exception:
        logger.exception("Bot connection error: {}...", bot_token[:10])
    finally:
        await state.bridge.unregister_bot(bot_token)
        await state.bridge.notify_bot_disconnected(bot_token)
