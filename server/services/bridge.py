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
_BOT_WORKERS_KEY = "bridge:bot_workers"        # HASH: token -> worker_id
_CHAT_COUNTS_PREFIX = "bridge:chats:"          # per-worker HASH: token -> count
_WORKERS_KEY = "bridge:workers"                # SET: known worker IDs
_PUBSUB_CHANNEL = "bridge:pubsub"
_WORKER_HEARTBEAT_PREFIX = "bridge:worker:"    # STRING key with TTL per worker

_WORKER_TTL = 30        # heartbeat TTL (seconds)
_HEARTBEAT_INTERVAL = 10  # how often each worker refreshes its heartbeat


class ConnectionBridge:
    """Manages the mapping between bot connections and chat clients.

    Each token can have one bot WebSocket and multiple chat WebSockets.
    Messages flow: chat -> server (JSON-RPC) -> bot -> server -> chat.
    Session state is persisted in Redis; WebSocket refs stay in-memory.

    Multi-worker safe: connection registry lives in Redis, cross-worker
    message routing uses Redis Pub/Sub.

    Worker liveness is tracked via per-worker heartbeat keys with a TTL.
    This ensures stale bot registrations from crashed or restarted workers
    are detected without requiring a startup cleanup — safe for rolling updates.
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
        # Background tasks
        self._pubsub_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._shutting_down = False

    async def start(self) -> None:
        """Start the Pub/Sub listener and worker heartbeat."""
        await self._redis.sadd(_WORKERS_KEY, self._worker_id)
        self._pubsub_task = asyncio.create_task(self._listen_pubsub())
        self._heartbeat_task = asyncio.create_task(self._run_heartbeat())
        logger.info("Bridge worker {} started", self._worker_id)

    async def _run_heartbeat(self) -> None:
        """Periodically refresh this worker's heartbeat key in Redis.

        As long as this key exists, other workers and the admin view treat
        this worker's bot registrations and chat counts as live.
        When the worker stops gracefully it deletes the key; when it crashes
        the key expires after _WORKER_TTL seconds.
        """
        while not self._shutting_down:
            try:
                await self._redis.set(
                    f"{_WORKER_HEARTBEAT_PREFIX}{self._worker_id}",
                    "1",
                    ex=_WORKER_TTL,
                )
                # Keep per-worker chat count key alive while this worker is up
                chat_key = f"{_CHAT_COUNTS_PREFIX}{self._worker_id}"
                if await self._redis.exists(chat_key):
                    await self._redis.expire(chat_key, _WORKER_TTL)
            except Exception:
                if not self._shutting_down:
                    logger.exception("Heartbeat refresh failed (worker={})", self._worker_id)
            await asyncio.sleep(_HEARTBEAT_INTERVAL)

    async def _is_worker_alive(self, worker_id: str) -> bool:
        """Return True if the given worker's heartbeat key is still present."""
        return bool(await self._redis.exists(f"{_WORKER_HEARTBEAT_PREFIX}{worker_id}"))

    def set_media_manager(self, media_manager) -> None:
        """Set the media manager for resolving download URLs in messages."""
        self._media_manager = media_manager

    # ── Bot registration (multi-worker safe) ─────────────────────────────────

    async def register_bot(self, token: str, ws: WebSocket) -> bool:
        """Register a bot connection. Returns False if a live bot is already connected.

        If Redis shows a bot registered but its owning worker's heartbeat has
        expired (crashed worker), the stale entry is cleaned up and registration
        proceeds.
        """
        if token in self._bots:
            return False

        if await self._redis.sismember(_ONLINE_BOTS_KEY, token):
            owner = await self._redis.hget(_BOT_WORKERS_KEY, token)
            if owner and await self._is_worker_alive(owner):
                return False  # Another live worker owns this bot
            # Owning worker is dead — clean up stale registration
            await self._redis.srem(_ONLINE_BOTS_KEY, token)
            await self._redis.hdel(_BOT_WORKERS_KEY, token)
            logger.warning(
                "Cleaned stale bot registration (dead worker={}, token={}...)",
                owner, token[:10],
            )

        self._bots[token] = ws
        await self._redis.sadd(_ONLINE_BOTS_KEY, token)
        await self._redis.hset(_BOT_WORKERS_KEY, token, self._worker_id)
        logger.info("Bot registered on worker {} (token={}...)", self._worker_id, token[:10])
        return True

    async def unregister_bot(self, token: str) -> None:
        """Remove bot from local dict + conditionally clean Redis (only if we own it)."""
        self._bots.pop(token, None)
        owner = await self._redis.hget(_BOT_WORKERS_KEY, token)
        if owner == self._worker_id:
            await self._redis.srem(_ONLINE_BOTS_KEY, token)
            await self._redis.hdel(_BOT_WORKERS_KEY, token)
            logger.info("Bot unregistered from Redis (worker={}, token={}...)", self._worker_id, token[:10])
        else:
            logger.info("Bot removed locally only (owner={}, self={}, token={}...)", owner, self._worker_id, token[:10])

    async def remove_bot_sessions(self, token: str) -> None:
        """Destroy Redis session data for a token. Called only on admin token delete."""
        await self._redis.delete(f"{_SESSIONS_PREFIX}{token}")
        await self._redis.delete(f"{_ACTIVE_PREFIX}{token}")
        await self._redis.srem(_ONLINE_BOTS_KEY, token)
        await self._redis.hdel(_BOT_WORKERS_KEY, token)
        # Remove chat counts for this token from all workers
        worker_ids = await self._redis.smembers(_WORKERS_KEY)
        for wid in worker_ids:
            await self._redis.hdel(f"{_CHAT_COUNTS_PREFIX}{wid}", token)
        logger.info("Bot sessions fully removed from Redis (token={}...)", token[:10])

    # ── Chat registration (multi-worker safe) ─────────────────────────────────

    async def register_chat(self, token: str, ws: WebSocket) -> None:
        if token not in self._chats:
            self._chats[token] = set()
        self._chats[token].add(ws)
        chat_key = f"{_CHAT_COUNTS_PREFIX}{self._worker_id}"
        await self._redis.hincrby(chat_key, token, 1)
        await self._redis.expire(chat_key, _WORKER_TTL)

    async def unregister_chat(self, token: str, ws: WebSocket) -> None:
        if token in self._chats:
            self._chats[token].discard(ws)
            if not self._chats[token]:
                del self._chats[token]
        chat_key = f"{_CHAT_COUNTS_PREFIX}{self._worker_id}"
        count = await self._redis.hincrby(chat_key, token, -1)
        if count <= 0:
            await self._redis.hdel(chat_key, token)

    # ── Queries (read from Redis for cluster-wide view) ───────────────────────

    async def is_bot_connected(self, token: str) -> bool:
        """Return True only if a bot is registered AND its owning worker is alive."""
        if not await self._redis.sismember(_ONLINE_BOTS_KEY, token):
            return False
        owner = await self._redis.hget(_BOT_WORKERS_KEY, token)
        if not owner:
            return False
        return await self._is_worker_alive(owner)

    async def get_connections_summary(self) -> dict[str, dict]:
        """Return per-token bot online status and chat connection count (cluster-wide).

        Both bot status and chat counts are verified against worker liveness
        to avoid showing stale data from crashed workers.
        """
        online_bots = await self._redis.smembers(_ONLINE_BOTS_KEY)
        worker_ids = await self._redis.smembers(_WORKERS_KEY)

        # Verify each bot's owning worker is still alive
        live_bots: set[str] = set()
        for token in online_bots:
            owner = await self._redis.hget(_BOT_WORKERS_KEY, token)
            if owner and await self._is_worker_alive(owner):
                live_bots.add(token)

        # Sum chat counts from alive workers only
        chat_counts: dict[str, int] = {}
        for wid in worker_ids:
            if not await self._is_worker_alive(wid):
                continue
            counts = await self._redis.hgetall(f"{_CHAT_COUNTS_PREFIX}{wid}")
            for token, count in counts.items():
                chat_counts[token] = chat_counts.get(token, 0) + int(count)

        tokens = live_bots | set(chat_counts.keys())
        summary: dict[str, dict] = {}
        for t in tokens:
            summary[t] = {
                "bot_online": t in live_bots,
                "chat_count": chat_counts.get(t, 0),
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

        # Delete this worker's heartbeat so other workers immediately see it as dead
        await self._redis.delete(f"{_WORKER_HEARTBEAT_PREFIX}{self._worker_id}")

        # Close all local bot connections
        for token, ws in list(self._bots.items()):
            try:
                await ws.close(code=4000, reason="Server restarting")
            except Exception:
                pass
            # Clean Redis if we own the bot
            owner = await self._redis.hget(_BOT_WORKERS_KEY, token)
            if owner == self._worker_id:
                await self._redis.srem(_ONLINE_BOTS_KEY, token)
                await self._redis.hdel(_BOT_WORKERS_KEY, token)
        self._bots.clear()

        # Close all local chat connections
        for token, chat_set in list(self._chats.items()):
            for ws in list(chat_set):
                try:
                    await ws.close(code=4000, reason="Server restarting")
                except Exception:
                    pass
        self._chats.clear()

        # Delete this worker's per-worker chat counts and workers SET entry
        await self._redis.delete(f"{_CHAT_COUNTS_PREFIX}{self._worker_id}")
        await self._redis.srem(_WORKERS_KEY, self._worker_id)

        self._pending_requests.clear()

        # Stop background tasks
        for task in (self._pubsub_task, self._heartbeat_task):
            if task:
                task.cancel()
                try:
                    await task
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
