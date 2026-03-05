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


@router.websocket("/bridge/chat")
async def ws_chat(
    ws: WebSocket,
    token: str = Query(default=""),
):
    if not await state.token_manager.validate(token):
        await ws.accept()
        await ws.close(code=4001, reason="Invalid or missing token")
        logger.warning("Chat connection rejected: invalid token {}...", token[:10])
        return

    await ws.accept()
    logger.info("Chat client connected: {}...", token[:10])

    await ws.send_json({
        "type": "bot_status",
        "connected": await state.bridge.is_bot_connected(token),
    })

    # Restore active session if one exists in Redis; otherwise create new
    existing_session = await state.bridge.get_active_session(token)
    if existing_session:
        sessions, active_id = await state.bridge.get_sessions(token)
        session_id = existing_session
        session_number = next((s[1] for s in sessions if s[0] == existing_session), 1)
        logger.info("Chat session restored: {} (token={}...)", session_id[:8], token[:10])
    else:
        session_id, session_number = await state.bridge.create_session(token)
        sessions, active_id = await state.bridge.get_sessions(token)

    # Register after session_id is known so inbox key uses the session_id
    await state.bridge.register_chat(token, ws, session_id)

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
                logger.info("Chat new_session requested (token={}...)", token[:10])
                new_id, new_num = await state.bridge.create_session(token)
                await state.bridge.update_chat_session(ws, new_id)
                session_id = new_id
                sessions, active_id = await state.bridge.get_sessions(token)
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
                logger.info("Chat switch_session requested: target={} (token={}...)", target_id[:8], token[:10])
                if await state.bridge.switch_session(token, target_id):
                    await state.bridge.update_chat_session(ws, target_id)
                    session_id = target_id
                    sessions, active_id = await state.bridge.get_sessions(token)
                    await ws.send_json({
                        "type": "switch_session_ack",
                        "sessionId": target_id,
                        "sessions": [{"id": s[0], "number": s[1]} for s in sessions],
                        "activeSessionId": active_id,
                    })
                else:
                    logger.warning("Chat switch_session failed: session {} not found (token={}...)", target_id[:8], token[:10])
                    await ws.send_json({
                        "type": "error",
                        "content": f"Session {target_id} not found",
                    })
                continue

            if msg_type == "message":
                msg_type_inner = data.get("msgType", "text")
                content = data.get("content", "")
                media = data.get("media")
                logger.info("Chat message received: type={} session={} (token={}...)", msg_type_inner, session_id[:8], token[:10])

                if msg_type_inner == "text" and not content:
                    logger.warning("Chat message rejected: empty text (token={}...)", token[:10])
                    await ws.send_json({"type": "error", "content": "Empty message"})
                    continue

                if msg_type_inner in ("image", "file", "audio", "video") and not media:
                    logger.warning("Chat message rejected: missing media for type={} (token={}...)", msg_type_inner, token[:10])
                    await ws.send_json({"type": "error", "content": "Missing media info"})
                    continue

                if not await state.bridge.is_bot_connected(token):
                    logger.warning("Chat message rejected: bot not connected (token={}...)", token[:10])
                    await ws.send_json({"type": "error", "content": "No bot connected"})
                    continue

                req_id = await state.bridge.send_to_bot(
                    token, content,
                    msg_type=msg_type_inner,
                    media=media,
                )
                if not req_id:
                    logger.warning("Chat send_to_bot failed (token={}...)", token[:10])
                    await ws.send_json({"type": "error", "content": "Failed to send to bot"})

    except WebSocketDisconnect:
        logger.info("Chat client disconnected: {}...", token[:10])
    except Exception:
        logger.exception("Chat connection error: {}...", token[:10])
    finally:
        await state.bridge.unregister_chat(token, ws)
