import asyncio
import json
import uuid
from typing import Optional

from fastapi import WebSocket
from redis.asyncio import Redis

from infra.log import logger

_SESSIONS_PREFIX = "bridge:sessions:"
_ACTIVE_PREFIX = "bridge:active:"
_ONLINE_BOTS_KEY = "bridge:online_bots"
_BOT_WORKER_PREFIX = "bridge:bot_worker:"
_CHAT_COUNTS_KEY = "bridge:chat_counts"
_PUBSUB_CHANNEL = "bridge:pubsub"


class ConnectionBridge:
    """Manages the mapping between bot connections and chat clients.

    Each token can have one bot WebSocket and multiple chat WebSockets.
    Messages flow: chat -> server (JSON-RPC) -> bot -> server -> chat.
    Session state is persisted in Redis; WebSocket refs stay in-memory.

    Multi-worker safe: connection registry lives in Redis, cross-worker
    message routing uses Redis Pub/Sub.
    """

    def __init__(self, redis: Redis):
        self._worker_id = uuid.uuid4().hex[:12]
        # token -> bot WebSocket (process-local)
        self._bots: dict[str, WebSocket] = {}
        # token -> set of chat WebSockets (process-local)
        self._chats: dict[str, set[WebSocket]] = {}
        # request_id -> token (process-local)
        self._pending_requests: dict[str, str] = {}
        # media manager reference
        self._media_manager = None
        # Redis client for session persistence + cross-worker state
        self._redis = redis
        # Pub/Sub listener task
        self._pubsub_task: Optional[asyncio.Task] = None
        self._shutting_down = False

    async def start(self) -> None:
        """Start the Pub/Sub listener for cross-worker messaging."""
        self._pubsub_task = asyncio.create_task(self._listen_pubsub())
        logger.info("Bridge worker {} started pub/sub listener", self._worker_id)

    def set_media_manager(self, media_manager) -> None:
        """Set the media manager for resolving download URLs in messages."""
        self._media_manager = media_manager

    # ── Bot registration (multi-worker safe) ─────────────────────────────────

    async def register_bot(self, token: str, ws: WebSocket) -> bool:
        """Register a bot connection. Returns False if a bot is already connected
        (locally or on another worker)."""
        if token in self._bots:
            return False
        # Check Redis global registry
        if await self._redis.sismember(_ONLINE_BOTS_KEY, token):
            return False
        self._bots[token] = ws
        await self._redis.sadd(_ONLINE_BOTS_KEY, token)
        await self._redis.set(f"{_BOT_WORKER_PREFIX}{token}", self._worker_id)
        logger.info("Bot registered on worker {} (token={}...)", self._worker_id, token[:10])
        return True

    async def unregister_bot(self, token: str) -> None:
        """Remove bot from local dict + conditionally clean Redis (only if we own it)."""
        self._bots.pop(token, None)
        # Only remove from Redis if this worker owns the bot registration
        owner = await self._redis.get(f"{_BOT_WORKER_PREFIX}{token}")
        if owner == self._worker_id:
            await self._redis.srem(_ONLINE_BOTS_KEY, token)
            await self._redis.delete(f"{_BOT_WORKER_PREFIX}{token}")
            logger.info("Bot unregistered from Redis (worker={}, token={}...)", self._worker_id, token[:10])
        else:
            logger.info("Bot removed locally only (owner={}, self={}, token={}...)", owner, self._worker_id, token[:10])

    async def remove_bot_sessions(self, token: str) -> None:
        """Destroy Redis session data for a token. Called only on admin token delete."""
        await self._redis.delete(f"{_SESSIONS_PREFIX}{token}")
        await self._redis.delete(f"{_ACTIVE_PREFIX}{token}")
        await self._redis.srem(_ONLINE_BOTS_KEY, token)
        await self._redis.delete(f"{_BOT_WORKER_PREFIX}{token}")
        await self._redis.hdel(_CHAT_COUNTS_KEY, token)
        logger.info("Bot sessions fully removed from Redis (token={}...)", token[:10])

    # ── Chat registration (multi-worker safe) ─────────────────────────────────

    async def register_chat(self, token: str, ws: WebSocket) -> None:
        if token not in self._chats:
            self._chats[token] = set()
        self._chats[token].add(ws)
        await self._redis.hincrby(_CHAT_COUNTS_KEY, token, 1)

    async def unregister_chat(self, token: str, ws: WebSocket) -> None:
        if token in self._chats:
            self._chats[token].discard(ws)
            if not self._chats[token]:
                del self._chats[token]
        count = await self._redis.hincrby(_CHAT_COUNTS_KEY, token, -1)
        if count <= 0:
            await self._redis.hdel(_CHAT_COUNTS_KEY, token)

    # ── Queries (read from Redis for cluster-wide view) ───────────────────────

    async def is_bot_connected(self, token: str) -> bool:
        return await self._redis.sismember(_ONLINE_BOTS_KEY, token)

    async def get_connections_summary(self) -> dict[str, dict]:
        """Return per-token bot online status and chat connection count (cluster-wide)."""
        online_bots = await self._redis.smembers(_ONLINE_BOTS_KEY)
        chat_counts = await self._redis.hgetall(_CHAT_COUNTS_KEY)
        tokens = set(online_bots) | set(chat_counts.keys())
        summary: dict[str, dict] = {}
        for t in tokens:
            summary[t] = {
                "bot_online": t in online_bots,
                "chat_count": int(chat_counts.get(t, 0)),
            }
        return summary

    # ── Session management ────────────────────────────────────────────────────

    async def create_session(self, token: str) -> tuple[str, int]:
        """Create a new session, append to Redis list, set as active."""
        session_id = str(uuid.uuid4())
        key = f"{_SESSIONS_PREFIX}{token}"
        await self._redis.rpush(key, session_id)
        await self._redis.set(f"{_ACTIVE_PREFIX}{token}", session_id)
        session_number = await self._redis.llen(key)
        logger.info("Session created: {} (token={}...)", session_id[:8], token[:10])
        return session_id, session_number

    async def get_active_session(self, token: str) -> Optional[str]:
        """Return the current active session ID, or None if no session exists."""
        return await self._redis.get(f"{_ACTIVE_PREFIX}{token}")

    async def reset_session(self, token: str) -> tuple[str, int]:
        """Reset the session for a token by creating a new one."""
        return await self.create_session(token)

    async def switch_session(self, token: str, session_id: str) -> bool:
        """Switch the active session. Returns False if session_id not found."""
        sessions = await self._redis.lrange(f"{_SESSIONS_PREFIX}{token}", 0, -1)
        if session_id not in sessions:
            logger.warning("Session switch failed: {} not found (token={}...)", session_id[:8], token[:10])
            return False
        await self._redis.set(f"{_ACTIVE_PREFIX}{token}", session_id)
        return True

    async def get_sessions(self, token: str) -> tuple[list[tuple[str, int]], str]:
        """Return ([(id, number), ...], active_id) for the token."""
        sessions = await self._redis.lrange(f"{_SESSIONS_PREFIX}{token}", 0, -1)
        numbered = [(sid, i + 1) for i, sid in enumerate(sessions)]
        active_id = await self._redis.get(f"{_ACTIVE_PREFIX}{token}") or ""
        return numbered, active_id

    # ── Message routing (cross-worker via Pub/Sub) ────────────────────────────

    async def send_to_bot(
        self,
        token: str,
        user_message: str,
        msg_type: str = "text",
        media: Optional[dict] = None,
    ) -> Optional[str]:
        """Create a JSON-RPC request and send it to the bot.
        If the bot is on another worker, route via Pub/Sub."""
        session_id = await self._redis.get(f"{_ACTIVE_PREFIX}{token}")
        if not session_id:
            session_id, _ = await self.create_session(token)

        request_id = f"req_{uuid.uuid4().hex[:12]}"
        self._pending_requests[request_id] = token

        # Build prompt content
        content_items = []
        if msg_type == "text":
            content_items.append({"type": "text", "text": user_message})
        elif msg_type in ("image", "file", "audio", "video"):
            media_info = {}
            if media:
                media_info = {
                    "mediaId": media.get("mediaId", ""),
                    "fileName": media.get("fileName", ""),
                    "mimeType": media.get("mimeType", ""),
                    "fileSize": media.get("fileSize", 0),
                    "downloadUrl": media.get("downloadUrl", ""),
                }
            description = user_message or f"[{msg_type}]"
            content_items.append({"type": "text", "text": description})
            content_items.append({
                "type": "media",
                "msgType": msg_type,
                "media": media_info,
            })
        else:
            content_items.append({"type": "text", "text": user_message})

        rpc_request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "session/prompt",
            "params": {
                "sessionId": session_id,
                "prompt": {
                    "content": content_items,
                },
            },
        }

        # Try local first
        bot_ws = self._bots.get(token)
        if bot_ws:
            try:
                await bot_ws.send_json(rpc_request)
                logger.info("Sent to bot (local): req={} type={} (token={}...)", request_id, msg_type, token[:10])
                return request_id
            except Exception:
                logger.exception("Failed to send to local bot (token={}...)", token[:10])
                self._pending_requests.pop(request_id, None)
                return None

        # Bot on another worker — route via Pub/Sub
        await self._publish({
            "action": "to_bot",
            "token": token,
            "rpc_request": rpc_request,
        })
        logger.info("Sent to bot (pub/sub): req={} type={} (token={}...)", request_id, msg_type, token[:10])
        return request_id

    async def handle_bot_message(self, token: str, raw: str) -> None:
        """Parse a JSON-RPC message from the bot and forward to chat clients."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON from bot (token={}...)", token[:10])
            return

        if msg.get("type") == "ping":
            return

        method = msg.get("method", "")
        params = msg.get("params", {})

        if method:
            chat_event = _translate_bot_event(method, params)
            if chat_event:
                await self._broadcast_to_chats(token, chat_event)

        if "id" in msg and "result" in msg:
            self._pending_requests.pop(msg["id"], None)
            done_event = _translate_bot_result(msg["result"])
            if done_event:
                await self._broadcast_to_chats(token, done_event)

        if "id" in msg and "error" in msg:
            self._pending_requests.pop(msg["id"], None)
            error_msg = msg["error"].get("message", "Unknown error from bot")
            logger.error("Bot JSON-RPC error: {} (token={}...)", error_msg, token[:10])
            error_event = {
                "type": "error",
                "content": error_msg,
            }
            await self._broadcast_to_chats(token, error_event)

    # ── Bot status notifications ──────────────────────────────────────────────

    async def notify_bot_connected(self, token: str) -> None:
        event = {"type": "bot_status", "connected": True}
        await self._broadcast_to_local_chats(token, event)
        await self._publish({"action": "bot_status", "token": token, "event": event})

    async def notify_bot_disconnected(self, token: str) -> None:
        event = {"type": "bot_status", "connected": False}
        await self._broadcast_to_local_chats(token, event)
        await self._publish({"action": "bot_status", "token": token, "event": event})

    # ── Broadcasting ──────────────────────────────────────────────────────────

    async def _broadcast_to_chats(self, token: str, event: dict) -> None:
        """Broadcast to local chats AND publish to Pub/Sub for other workers."""
        await self._broadcast_to_local_chats(token, event)
        await self._publish({"action": "to_chats", "token": token, "event": event})

    async def _broadcast_to_local_chats(self, token: str, event: dict) -> None:
        """Broadcast to chat WebSockets on this worker only."""
        chat_set = self._chats.get(token)
        if not chat_set:
            return
        payload = json.dumps(event)
        closed: list[WebSocket] = []
        for ws in chat_set:
            try:
                await ws.send_text(payload)
            except Exception:
                closed.append(ws)
        for ws in closed:
            chat_set.discard(ws)
        if closed:
            logger.warning("Removed {} dead chat connections (token={}...)", len(closed), token[:10])

    # ── Pub/Sub ───────────────────────────────────────────────────────────────

    async def _publish(self, message: dict) -> None:
        """Publish a message to the Pub/Sub channel with origin worker ID."""
        message["_origin"] = self._worker_id
        try:
            await self._redis.publish(_PUBSUB_CHANNEL, json.dumps(message))
        except Exception:
            if not self._shutting_down:
                logger.exception("Failed to publish to Pub/Sub")

    async def _listen_pubsub(self) -> None:
        """Subscribe to the Pub/Sub channel and handle messages from other workers."""
        while not self._shutting_down:
            try:
                pubsub = self._redis.pubsub()
                await pubsub.subscribe(_PUBSUB_CHANNEL)
                logger.info("Worker {} subscribed to {}", self._worker_id, _PUBSUB_CHANNEL)
                async for message in pubsub.listen():
                    if self._shutting_down:
                        break
                    if message["type"] != "message":
                        continue
                    try:
                        data = json.loads(message["data"])
                    except (json.JSONDecodeError, TypeError):
                        continue
                    # Skip messages from this worker
                    if data.get("_origin") == self._worker_id:
                        continue
                    await self._handle_pubsub(data)
            except asyncio.CancelledError:
                break
            except Exception:
                if not self._shutting_down:
                    logger.exception("Pub/Sub listener error, reconnecting in 1s...")
                    await asyncio.sleep(1)

    async def _handle_pubsub(self, data: dict) -> None:
        """Handle an incoming Pub/Sub message from another worker."""
        action = data.get("action")
        token = data.get("token", "")

        if action == "to_bot":
            # Another worker wants us to forward a message to a bot we own
            bot_ws = self._bots.get(token)
            if bot_ws:
                try:
                    await bot_ws.send_json(data["rpc_request"])
                    logger.info("Pub/Sub: forwarded to local bot (token={}...)", token[:10])
                except Exception:
                    logger.exception("Pub/Sub: failed to forward to local bot (token={}...)", token[:10])

        elif action == "to_chats":
            # Another worker is broadcasting to chats — send to our local ones
            await self._broadcast_to_local_chats(token, data["event"])

        elif action == "bot_status":
            # Bot connected/disconnected on another worker — notify our local chats
            await self._broadcast_to_local_chats(token, data["event"])

    # ── Graceful shutdown ─────────────────────────────────────────────────────

    async def shutdown(self) -> None:
        """Graceful shutdown: close all local WebSocket connections, clean up Redis."""
        self._shutting_down = True
        logger.info("Bridge worker {} shutting down...", self._worker_id)

        # Close all local bot connections
        for token, ws in list(self._bots.items()):
            try:
                await ws.close(code=4000, reason="Server restarting")
            except Exception:
                pass
            # Clean Redis if we own the bot
            owner = await self._redis.get(f"{_BOT_WORKER_PREFIX}{token}")
            if owner == self._worker_id:
                await self._redis.srem(_ONLINE_BOTS_KEY, token)
                await self._redis.delete(f"{_BOT_WORKER_PREFIX}{token}")
        self._bots.clear()

        # Close all local chat connections
        for token, chat_set in list(self._chats.items()):
            for ws in list(chat_set):
                try:
                    await ws.close(code=4000, reason="Server restarting")
                except Exception:
                    pass
            # Decrement chat counts
            local_count = len(chat_set)
            if local_count > 0:
                new_count = await self._redis.hincrby(_CHAT_COUNTS_KEY, token, -local_count)
                if new_count <= 0:
                    await self._redis.hdel(_CHAT_COUNTS_KEY, token)
        self._chats.clear()

        self._pending_requests.clear()

        # Stop Pub/Sub listener
        if self._pubsub_task:
            self._pubsub_task.cancel()
            try:
                await self._pubsub_task
            except asyncio.CancelledError:
                pass

        logger.info("Bridge worker {} shutdown complete", self._worker_id)


def _translate_bot_event(method: str, params: dict) -> Optional[dict]:
    """Convert a bot JSON-RPC notification to a simplified chat event."""
    if method == "session/update":
        update = params.get("update", {})
        update_type = update.get("sessionUpdate", "")
        content = update.get("content", {})

        if update_type == "agent_message_chunk":
            return {"type": "chunk", "content": content.get("text", "")}
        if update_type == "agent_message_final":
            return {"type": "done", "content": content.get("text", "")}
        if update_type == "tool_result":
            logger.info("TOOL_RESULT update: {}", json.dumps(update, ensure_ascii=False)[:500])
            result_text = update.get("content", "")
            if not isinstance(result_text, str):
                if isinstance(result_text, dict):
                    result_text = result_text.get("text", "")
                else:
                    result_text = json.dumps(result_text) if result_text else ""
            title = update.get("title", "tool")
            status = update.get("status", "completed")
            return {"type": "tool_result", "name": title, "status": status, "content": result_text}
        if update_type == "agent_thought_chunk":
            return {"type": "thinking", "content": content.get("text", "")}
        if update_type == "tool_call":
            logger.info("TOOL_CALL update: {}", json.dumps(update, ensure_ascii=False)[:500])
            title = update.get("title", "tool")
            input_text = update.get("content", "")
            if not isinstance(input_text, str):
                input_text = json.dumps(input_text) if input_text else ""
            return {"type": "tool_call", "name": title, "input": input_text}

        if update_type == "agent_media":
            media = content.get("media", {})
            return {
                "type": "message",
                "msgType": content.get("msgType", "file"),
                "content": content.get("text", ""),
                "media": {
                    "mediaId": media.get("mediaId", ""),
                    "fileName": media.get("fileName", ""),
                    "mimeType": media.get("mimeType", ""),
                    "fileSize": media.get("fileSize", 0),
                    "downloadUrl": f"/api/media/download/{media.get('mediaId', '')}",
                },
            }

        if isinstance(content, dict) and "text" in content:
            return {"type": "chunk", "content": content["text"]}
        return None

    return None


def _translate_bot_result(result: dict) -> Optional[dict]:
    """Convert a JSON-RPC result (prompt completion) to a chat event."""
    stop_reason = result.get("stopReason", "")
    if stop_reason:
        return {"type": "done"}
    return None
