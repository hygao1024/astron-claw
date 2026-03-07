from __future__ import annotations

import asyncio
import json
import uuid
from typing import TYPE_CHECKING, Optional

from redis.asyncio import Redis

from infra.log import logger

if TYPE_CHECKING:
    from fastapi import WebSocket

    from services.queue import MessageQueue
    from services.session_store import SessionStore

_ONLINE_BOTS_KEY = "bridge:online_bots"
_BOT_WORKERS_KEY = "bridge:bot_workers"        # HASH: token -> worker_id
_WORKERS_KEY = "bridge:workers"                # SET: known worker IDs
_BOT_INBOX_PREFIX = "bridge:bot_inbox:"        # STREAM per token: messages TO bot
CHAT_INBOX_PREFIX = "bridge:chat_inbox:"       # STREAM per chat: messages TO chat (shared with sse.py)
_WORKER_HEARTBEAT_PREFIX = "bridge:worker:"    # STRING key with TTL per worker

_WORKER_TTL = 30         # heartbeat TTL (seconds)
_HEARTBEAT_INTERVAL = 10 # how often each worker refreshes its heartbeat
_CONSUME_BLOCK_MS = 5000 # XREADGROUP block timeout (milliseconds)


class ConnectionBridge:
    """Manages the mapping between bot connections and chat clients.

    Each token can have one bot WebSocket.
    Messages flow: chat (SSE) -> server (JSON-RPC) -> bot -> server -> chat (SSE).
    Session data is persisted in MySQL via SessionStore, with Redis as a
    write-through cache. Bot WebSocket refs stay in-memory.

    Multi-worker safe: connection registry lives in Redis, cross-worker
    message routing uses per-token Redis Streams (XADD / XREADGROUP),
    compatible with both standalone and cluster modes.

    Worker liveness is tracked via per-worker heartbeat keys with a TTL.
    This ensures stale bot registrations from crashed or restarted workers
    are detected without requiring a startup cleanup — safe for rolling updates.
    """

    def __init__(
        self,
        redis: Redis,
        session_store: SessionStore,
        queue: MessageQueue,
    ):
        self._worker_id = uuid.uuid4().hex[:12]
        # token -> bot WebSocket (process-local)
        self._bots: dict[str, WebSocket] = {}
        # request_id -> (token, session_id) for targeted response routing
        self._pending_requests: dict[str, tuple[str, str]] = {}
        # media manager reference
        self._media_manager = None
        # Redis client for cross-worker state
        self._redis = redis
        # Session persistence layer (MySQL + Redis cache)
        self._session_store = session_store
        # Message queue abstraction (Redis Streams)
        self._queue = queue
        # Per-connection consume tasks: task_key -> asyncio.Task
        self._poll_tasks: dict[str, asyncio.Task] = {}
        # Background tasks
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._shutting_down = False

    async def start(self) -> None:
        """Start the worker heartbeat."""
        await self._redis.sadd(_WORKERS_KEY, self._worker_id)
        self._heartbeat_task = asyncio.create_task(self._run_heartbeat())
        logger.info("Bridge worker {} started", self._worker_id)

    async def _run_heartbeat(self) -> None:
        """Periodically refresh this worker's heartbeat key in Redis."""
        while not self._shutting_down:
            try:
                await self._redis.set(
                    f"{_WORKER_HEARTBEAT_PREFIX}{self._worker_id}",
                    "1",
                    ex=_WORKER_TTL,
                )
                # Re-sync this worker's presence in workers SET
                await self._redis.sadd(_WORKERS_KEY, self._worker_id)
                # Re-sync local bot registrations into Redis
                for token in self._bots:
                    await self._redis.sadd(_ONLINE_BOTS_KEY, token)
                    await self._redis.hset(_BOT_WORKERS_KEY, token, self._worker_id)
                # Clean stale worker IDs from workers SET
                all_workers = await self._redis.smembers(_WORKERS_KEY)
                for wid in all_workers:
                    wid_str = wid if isinstance(wid, str) else wid.decode()
                    if wid_str != self._worker_id and not await self._is_worker_alive(wid_str):
                        await self._redis.srem(_WORKERS_KEY, wid_str)
                # Clean stale bot registrations owned by dead workers
                bot_tokens = await self._redis.hgetall(_BOT_WORKERS_KEY)
                for tok, owner in bot_tokens.items():
                    tok_str = tok if isinstance(tok, str) else tok.decode()
                    owner_str = owner if isinstance(owner, str) else owner.decode()
                    if owner_str != self._worker_id and not await self._is_worker_alive(owner_str):
                        await self._redis.srem(_ONLINE_BOTS_KEY, tok_str)
                        await self._redis.hdel(_BOT_WORKERS_KEY, tok_str)
                        await self._queue.delete_queue(f"{_BOT_INBOX_PREFIX}{tok_str}")
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
        """Register a bot connection. Returns False if a live bot is already connected."""
        if token in self._bots:
            return False

        if await self._redis.sismember(_ONLINE_BOTS_KEY, token):
            owner = await self._redis.hget(_BOT_WORKERS_KEY, token)
            if owner and await self._is_worker_alive(owner):
                return False
            await self._redis.srem(_ONLINE_BOTS_KEY, token)
            await self._redis.hdel(_BOT_WORKERS_KEY, token)
            logger.warning(
                "Cleaned stale bot registration (dead worker={}, token={}...)",
                owner, token[:10],
            )

        self._bots[token] = ws
        await self._redis.sadd(_ONLINE_BOTS_KEY, token)
        await self._redis.hset(_BOT_WORKERS_KEY, token, self._worker_id)
        # Ensure consumer group exists and start consuming bot inbox
        inbox = f"{_BOT_INBOX_PREFIX}{token}"
        await self._queue.ensure_group(inbox, "bot")
        task_key = f"bot:{token}"
        self._poll_tasks[task_key] = asyncio.create_task(self._poll_bot_inbox(token))
        logger.info("Bot registered on worker {} (token={}...)", self._worker_id, token[:10])
        return True

    async def unregister_bot(self, token: str) -> None:
        """Remove bot from local dict + clean up Redis and inbox."""
        self._bots.pop(token, None)
        # Stop bot inbox consuming
        task_key = f"bot:{token}"
        task = self._poll_tasks.pop(task_key, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._queue.delete_queue(f"{_BOT_INBOX_PREFIX}{token}")
        owner = await self._redis.hget(_BOT_WORKERS_KEY, token)
        if owner == self._worker_id:
            await self._redis.srem(_ONLINE_BOTS_KEY, token)
            await self._redis.hdel(_BOT_WORKERS_KEY, token)
            logger.info("Bot unregistered from Redis (worker={}, token={}...)", self._worker_id, token[:10])
        else:
            logger.info("Bot removed locally only (owner={}, self={}, token={}...)", owner, self._worker_id, token[:10])

    async def remove_bot_sessions(self, token: str) -> None:
        """Destroy session data for a token. Called only on admin token delete."""
        await self._session_store.remove_sessions(token)

        # Disconnect local bot if on this worker
        if token in self._bots:
            bot_ws = self._bots[token]
            try:
                await bot_ws.close(code=4003, reason="Token deleted")
            except Exception:
                pass
            await self.unregister_bot(token)
        else:
            # Bot may be on a remote worker — push disconnect command to inbox
            inbox = f"{_BOT_INBOX_PREFIX}{token}"
            await self._queue.publish(inbox, json.dumps({"_disconnect": True}))

        # Clean remaining Redis keys
        await self._redis.srem(_ONLINE_BOTS_KEY, token)
        await self._redis.hdel(_BOT_WORKERS_KEY, token)
        await self._queue.delete_queue(f"{_BOT_INBOX_PREFIX}{token}")
        logger.info("Bot sessions fully removed (token={}...)", token[:10])

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
        """Return per-token bot online status (cluster-wide)."""
        online_bots = await self._redis.smembers(_ONLINE_BOTS_KEY)
        if not online_bots:
            return {}

        tokens = list(online_bots)

        # Batch: fetch owner worker_id for each token
        pipe = self._redis.pipeline()
        for t in tokens:
            pipe.hget(_BOT_WORKERS_KEY, t)
        owners = await pipe.execute()

        # Batch: check heartbeat for each unique owner
        unique_owners = {o for o in owners if o}
        alive_cache: dict[str, bool] = {}
        if unique_owners:
            pipe2 = self._redis.pipeline()
            owner_list = list(unique_owners)
            for o in owner_list:
                o_str = o if isinstance(o, str) else o.decode()
                pipe2.exists(f"{_WORKER_HEARTBEAT_PREFIX}{o_str}")
            alive_results = await pipe2.execute()
            for o, alive in zip(owner_list, alive_results):
                o_str = o if isinstance(o, str) else o.decode()
                alive_cache[o_str] = bool(alive)

        summary: dict[str, dict] = {}
        for t, owner in zip(tokens, owners):
            if not owner:
                continue
            o_str = owner if isinstance(owner, str) else owner.decode()
            if alive_cache.get(o_str, False):
                summary[t] = {"bot_online": True}
        return summary

    # ── Session management (delegated to SessionStore) ─────────────────────

    async def create_session(self, token: str) -> tuple[str, int]:
        """Create a new session, persist to MySQL, cache in Redis."""
        session_id = str(uuid.uuid4())
        session_number = await self._session_store.create_session(token, session_id)
        logger.info("Session created: {} (token={}...)", session_id[:8], token[:10])
        return session_id, session_number

    async def get_active_session(self, token: str) -> Optional[str]:
        return await self._session_store.get_active_session(token)

    async def reset_session(self, token: str) -> tuple[str, int]:
        return await self.create_session(token)

    async def switch_session(self, token: str, session_id: str) -> bool:
        return await self._session_store.switch_session(token, session_id)

    async def get_sessions(self, token: str) -> tuple[list[tuple[str, int]], str]:
        return await self._session_store.get_sessions(token)

    async def cleanup_old_sessions(self, max_age_days: float) -> int:
        return await self._session_store.cleanup_old_sessions(max_age_days * 86400)

    # ── Message routing (cross-worker via per-token inbox) ─────────────────

    async def send_to_bot(
        self,
        token: str,
        user_message: str,
        msg_type: str = "text",
        media: Optional[dict] = None,
        session_id: Optional[str] = None,
    ) -> Optional[str]:
        """Create a JSON-RPC request and send it to the bot.

        If session_id is provided it is used directly (avoids race conditions
        when multiple sessions send concurrently).  Otherwise falls back to
        get_active_session / create_session.
        """
        if not session_id:
            session_id = await self.get_active_session(token)
            if not session_id:
                session_id, _ = await self.create_session(token)

        request_id = f"req_{uuid.uuid4().hex[:12]}"
        self._pending_requests[request_id] = (token, session_id)

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

        # Always route via inbox (works for both local and remote workers)
        try:
            inbox = f"{_BOT_INBOX_PREFIX}{token}"
            await self._queue.publish(inbox, json.dumps({"rpc_request": rpc_request}))
        except Exception:
            logger.exception("Failed to push to bot inbox (token={}...)", token[:10])
            self._pending_requests.pop(request_id, None)
            return None
        logger.info("Sent to bot (inbox): req={} type={} (token={}...)", request_id, msg_type, token[:10])
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
            # Notifications carry sessionId in params; route to that session
            session_id = params.get("sessionId") if params else None
            if not session_id:
                session_id = await self.get_active_session(token)
            # Chunk events are high-frequency — use DEBUG to avoid flooding INFO
            if chat_event and chat_event.get("type") in ("chunk", "thinking"):
                logger.debug("Bot event: method={} type={} session={} (token={}...)", method, chat_event["type"], session_id[:8] if session_id else "?", token[:10])
            else:
                logger.info("Bot event: method={} session={} (token={}...)", method, session_id[:8] if session_id else "?", token[:10])
            if chat_event:
                if session_id:
                    await self._send_to_session(token, session_id, chat_event)
            else:
                logger.warning("Bot event dropped: method={} untranslatable (token={}...)", method, token[:10])

        if "id" in msg and "result" in msg:
            info = self._pending_requests.pop(msg["id"], None)
            session_id = info[1] if info else await self.get_active_session(token)
            logger.info("Bot result: req={} session={} (token={}...)", msg["id"], session_id[:8] if session_id else "?", token[:10])

        if "id" in msg and "error" in msg:
            info = self._pending_requests.pop(msg["id"], None)
            error_msg = msg["error"].get("message", "Unknown error from bot")
            logger.error("Bot JSON-RPC error: {} (token={}...)", error_msg, token[:10])
            error_event = {"type": "error", "content": error_msg}
            session_id = info[1] if info else await self.get_active_session(token)
            if session_id:
                await self._send_to_session(token, session_id, error_event)

    # ── Bot status notifications ──────────────────────────────────────────────

    async def notify_bot_connected(self, token: str) -> None:
        logger.info("Bot status -> connected (token={}...)", token[:10])
        session_id = await self.get_active_session(token)
        if session_id:
            await self._send_to_session(token, session_id, {"type": "bot_status", "connected": True})

    async def notify_bot_disconnected(self, token: str) -> None:
        logger.info("Bot status -> disconnected (token={}...)", token[:10])
        session_id = await self.get_active_session(token)
        if session_id:
            await self._send_to_session(token, session_id, {"type": "bot_status", "connected": False})

    # ── Broadcasting ──────────────────────────────────────────────────────────

    async def _send_to_session(self, token: str, session_id: str, event: dict) -> None:
        """Send event to a specific session's chat inbox via Redis Stream."""
        try:
            inbox = f"{CHAT_INBOX_PREFIX}{token}:{session_id}"
            await self._queue.publish(inbox, json.dumps(event))
            logger.debug("Event pushed to session inbox: type={} session={} (token={}...)", event.get("type"), session_id[:8], token[:10])
        except Exception:
            if not self._shutting_down:
                logger.exception("Failed to send to session inbox (token={}... session={}...)", token[:10], session_id[:8])

    # ── Per-connection inbox consuming ───────────────────────────────────────

    async def _poll_bot_inbox(self, token: str) -> None:
        """Consume bot_inbox:{token} via XREADGROUP and forward to the local bot WS."""
        inbox = f"{_BOT_INBOX_PREFIX}{token}"
        while not self._shutting_down:
            try:
                result = await self._queue.consume(
                    inbox, group="bot", consumer="bot",
                    block_ms=_CONSUME_BLOCK_MS,
                )
                if result is None:
                    # In production XREADGROUP BLOCK waits at Redis level;
                    # yield to event loop as a safety net for mocked/non-blocking paths.
                    await asyncio.sleep(0)
                    continue
                msg_id, raw = result
                data = json.loads(raw)
                await self._queue.ack(inbox, "bot", msg_id)
                # Handle disconnect command from admin token delete
                if data.get("_disconnect"):
                    bot_ws = self._bots.get(token)
                    if bot_ws:
                        try:
                            await bot_ws.close(code=4003, reason="Token deleted")
                        except Exception:
                            pass
                    logger.info("Inbox: received disconnect for bot (token={}...)", token[:10])
                    break
                bot_ws = self._bots.get(token)
                if bot_ws:
                    await bot_ws.send_json(data["rpc_request"])
                    logger.info("Inbox: forwarded to local bot (token={}...)", token[:10])
                else:
                    logger.warning("Inbox: bot WS gone, message dropped (token={}...)", token[:10])
            except asyncio.CancelledError:
                break
            except Exception:
                if not self._shutting_down:
                    logger.exception("Bot inbox consume error (token={}...)", token[:10])
                    await asyncio.sleep(1)

    # ── Graceful shutdown ─────────────────────────────────────────────────────

    async def shutdown(self) -> None:
        """Graceful shutdown: close all connections, clean up Redis."""
        self._shutting_down = True
        logger.info("Bridge worker {} shutting down...", self._worker_id)

        await self._redis.delete(f"{_WORKER_HEARTBEAT_PREFIX}{self._worker_id}")

        # Close bot connections and clean Redis
        for token, ws in list(self._bots.items()):
            try:
                await ws.close(code=4000, reason="Server restarting")
            except Exception:
                pass
            owner = await self._redis.hget(_BOT_WORKERS_KEY, token)
            if owner == self._worker_id:
                await self._redis.srem(_ONLINE_BOTS_KEY, token)
                await self._redis.hdel(_BOT_WORKERS_KEY, token)
            await self._queue.delete_queue(f"{_BOT_INBOX_PREFIX}{token}")
        self._bots.clear()

        await self._redis.srem(_WORKERS_KEY, self._worker_id)
        self._pending_requests.clear()

        # Cancel all polling tasks
        for task in self._poll_tasks.values():
            task.cancel()
        for task in self._poll_tasks.values():
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._poll_tasks.clear()

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
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
            logger.debug("Bot event fallback to chunk: sessionUpdate={} (unknown type)", update_type)
            return {"type": "chunk", "content": content["text"]}
        logger.warning("Bot event untranslatable: sessionUpdate={}", update_type)
        return None

    return None

