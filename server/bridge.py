import json
import uuid
import logging
from typing import Optional
from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionBridge:
    """Manages the mapping between bot connections and chat clients.

    Each token can have one bot WebSocket and multiple chat WebSockets.
    Messages flow: chat -> server (JSON-RPC) -> bot -> server -> chat.
    """

    def __init__(self):
        # token -> bot WebSocket
        self._bots: dict[str, WebSocket] = {}
        # token -> set of chat WebSockets
        self._chats: dict[str, set[WebSocket]] = {}
        # request_id -> token (to route bot responses back)
        self._pending_requests: dict[str, str] = {}
        # token -> ordered list of session IDs
        self._session_list: dict[str, list[str]] = {}
        # token -> currently active session ID
        self._active_session: dict[str, str] = {}
        # media manager reference (set via set_media_manager)
        self._media_manager = None

    def set_media_manager(self, media_manager) -> None:
        """Set the media manager for resolving download URLs in messages."""
        self._media_manager = media_manager

    def register_bot(self, token: str, ws: WebSocket) -> bool:
        """Register a bot connection. Returns False if a bot is already connected."""
        if token in self._bots:
            return False
        self._bots[token] = ws
        return True

    def unregister_bot(self, token: str) -> None:
        self._bots.pop(token, None)
        self._session_list.pop(token, None)
        self._active_session.pop(token, None)

    def register_chat(self, token: str, ws: WebSocket) -> None:
        if token not in self._chats:
            self._chats[token] = set()
        self._chats[token].add(ws)

    def unregister_chat(self, token: str, ws: WebSocket) -> None:
        if token in self._chats:
            self._chats[token].discard(ws)
            if not self._chats[token]:
                del self._chats[token]

    def create_session(self, token: str) -> tuple[str, int]:
        """Create a new session, append to list, set as active. Returns (session_id, session_number)."""
        session_id = str(uuid.uuid4())
        if token not in self._session_list:
            self._session_list[token] = []
        self._session_list[token].append(session_id)
        self._active_session[token] = session_id
        session_number = len(self._session_list[token])
        return session_id, session_number

    def reset_session(self, token: str) -> tuple[str, int]:
        """Reset the session for a token by creating a new one. Returns (session_id, session_number)."""
        return self.create_session(token)

    def switch_session(self, token: str, session_id: str) -> bool:
        """Switch the active session. Returns False if session_id not found."""
        sessions = self._session_list.get(token, [])
        if session_id not in sessions:
            return False
        self._active_session[token] = session_id
        return True

    def get_sessions(self, token: str) -> tuple[list[tuple[str, int]], str]:
        """Return ([(id, number), ...], active_id) for the token."""
        sessions = self._session_list.get(token, [])
        numbered = [(sid, i + 1) for i, sid in enumerate(sessions)]
        active_id = self._active_session.get(token, "")
        return numbered, active_id

    def is_bot_connected(self, token: str) -> bool:
        return token in self._bots

    def create_session(self, token: str) -> tuple[str, int]:
        """Create a new session, append to list, set as active. Returns (session_id, session_number)."""
        session_id = str(uuid.uuid4())
        if token not in self._session_list:
            self._session_list[token] = []
        self._session_list[token].append(session_id)
        self._active_session[token] = session_id
        session_number = len(self._session_list[token])
        return session_id, session_number

    def reset_session(self, token: str) -> tuple[str, int]:
        """Reset the session for a token by creating a new one."""
        return self.create_session(token)

    def switch_session(self, token: str, session_id: str) -> bool:
        """Switch the active session. Returns False if session_id not found."""
        sessions = self._session_list.get(token, [])
        if session_id not in sessions:
            return False
        self._active_session[token] = session_id
        return True

    def get_sessions(self, token: str) -> tuple[list[tuple[str, int]], str]:
        """Return ([(id, number), ...], active_id) for the token."""
        sessions = self._session_list.get(token, [])
        numbered = [(sid, i + 1) for i, sid in enumerate(sessions)]
        active_id = self._active_session.get(token, "")
        return numbered, active_id

    async def send_to_bot(
        self,
        token: str,
        user_message: str,
        msg_type: str = "text",
        media: Optional[dict] = None,
    ) -> Optional[str]:
        """Create a JSON-RPC request and send it to the bot. Returns request_id."""
        bot_ws = self._bots.get(token)
        if not bot_ws:
            return None

        session_id = self._active_session.get(token)
        if not session_id:
            session_id, _ = self.create_session(token)

        request_id = f"req_{uuid.uuid4().hex[:12]}"
        self._pending_requests[request_id] = token

        # Build prompt content based on message type
        content_items = []

        if msg_type == "text":
            content_items.append({"type": "text", "text": user_message})
        elif msg_type in ("image", "file", "audio", "video"):
            # Include media metadata in the prompt
            media_info = {}
            if media:
                media_info = {
                    "mediaId": media.get("mediaId", ""),
                    "fileName": media.get("fileName", ""),
                    "mimeType": media.get("mimeType", ""),
                    "fileSize": media.get("fileSize", 0),
                    "downloadUrl": media.get("downloadUrl", ""),
                }

            # For media messages, include a text description and media reference
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

        try:
            await bot_ws.send_json(rpc_request)
            return request_id
        except Exception:
            logger.exception("Failed to send to bot for token %s", token[:10])
            self._pending_requests.pop(request_id, None)
            return None

    async def handle_bot_message(self, token: str, raw: str) -> None:
        """Parse a JSON-RPC message from the bot and forward to chat clients."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON from bot for token %s", token[:10])
            return

        # Handle ping from plugin
        if msg.get("type") == "ping":
            return

        # JSON-RPC notification (no id) - typically session/update
        method = msg.get("method", "")
        params = msg.get("params", {})

        if method:
            chat_event = _translate_bot_event(method, params)
            if chat_event:
                await self._broadcast_to_chats(token, chat_event)

        # If it's a JSON-RPC response (has id + result), the prompt is complete
        if "id" in msg and "result" in msg:
            self._pending_requests.pop(msg["id"], None)
            done_event = _translate_bot_result(msg["result"])
            if done_event:
                await self._broadcast_to_chats(token, done_event)

        # If it's a JSON-RPC error response
        if "id" in msg and "error" in msg:
            self._pending_requests.pop(msg["id"], None)
            error_event = {
                "type": "error",
                "content": msg["error"].get("message", "Unknown error from bot"),
            }
            await self._broadcast_to_chats(token, error_event)

    def get_connections_summary(self) -> dict[str, dict]:
        """Return per-token bot online status and chat connection count."""
        tokens: set[str] = set(self._bots.keys()) | set(self._chats.keys())
        summary: dict[str, dict] = {}
        for t in tokens:
            summary[t] = {
                "bot_online": t in self._bots,
                "chat_count": len(self._chats.get(t, set())),
            }
        return summary

    async def notify_bot_connected(self, token: str) -> None:
        event = {"type": "bot_status", "connected": True}
        await self._broadcast_to_chats(token, event)

    async def notify_bot_disconnected(self, token: str) -> None:
        event = {"type": "bot_status", "connected": False}
        await self._broadcast_to_chats(token, event)

    async def _broadcast_to_chats(self, token: str, event: dict) -> None:
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
            logger.info("TOOL_RESULT update: %s", json.dumps(update, ensure_ascii=False)[:500])
            return {"type": "tool_result", "content": content.get("text", "")}
        if update_type == "agent_thought_chunk":
            return {"type": "thinking", "content": content.get("text", "")}
        if update_type == "tool_call":
            logger.info("TOOL_CALL update: %s", json.dumps(update, ensure_ascii=False)[:500])
            title = update.get("title", "tool")
            tool_content = update.get("content", [])
            input_text = ""
            if tool_content and isinstance(tool_content, list):
                for item in tool_content:
                    inner = item.get("content", {}) if isinstance(item, dict) else {}
                    if isinstance(inner, dict):
                        input_text += inner.get("text", "")
            return {"type": "tool_call", "name": title, "input": input_text}

        # Handle media messages from bot
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

        # Forward unrecognized update types as generic chunks
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
