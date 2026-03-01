import asyncio
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
        # token -> current session id
        self._sessions: dict[str, str] = {}

    def register_bot(self, token: str, ws: WebSocket) -> bool:
        """Register a bot connection. Returns False if a bot is already connected."""
        if token in self._bots:
            return False
        self._bots[token] = ws
        return True

    def unregister_bot(self, token: str) -> None:
        self._bots.pop(token, None)
        self._sessions.pop(token, None)

    def register_chat(self, token: str, ws: WebSocket) -> None:
        if token not in self._chats:
            self._chats[token] = set()
        self._chats[token].add(ws)

    def unregister_chat(self, token: str, ws: WebSocket) -> None:
        if token in self._chats:
            self._chats[token].discard(ws)
            if not self._chats[token]:
                del self._chats[token]

    def is_bot_connected(self, token: str) -> bool:
        return token in self._bots

    async def send_to_bot(self, token: str, user_message: str) -> Optional[str]:
        """Create a JSON-RPC request and send it to the bot. Returns request_id."""
        bot_ws = self._bots.get(token)
        if not bot_ws:
            return None

        session_id = self._sessions.get(token)
        if not session_id:
            session_id = str(uuid.uuid4())
            self._sessions[token] = session_id

        request_id = f"req_{uuid.uuid4().hex[:12]}"
        self._pending_requests[request_id] = token

        rpc_request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "session/prompt",
            "params": {
                "sessionId": session_id,
                "prompt": {
                    "content": [{"type": "text", "text": user_message}]
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
        if update_type == "agent_thought_chunk":
            return {"type": "thinking", "content": content.get("text", "")}
        if update_type == "tool_call":
            # Plugin sends: toolCallId, title, status, content[{type,content:{type,text}}]
            title = update.get("title", "tool")
            tool_content = update.get("content", [])
            input_text = ""
            if tool_content and isinstance(tool_content, list):
                for item in tool_content:
                    inner = item.get("content", {}) if isinstance(item, dict) else {}
                    if isinstance(inner, dict):
                        input_text += inner.get("text", "")
            return {"type": "tool_call", "name": title, "input": input_text}
        if update_type == "tool_call_update":
            title = update.get("title", "tool")
            tool_content = update.get("content", [])
            result_text = ""
            if tool_content and isinstance(tool_content, list):
                for item in tool_content:
                    inner = item.get("content", {}) if isinstance(item, dict) else {}
                    if isinstance(inner, dict):
                        result_text += inner.get("text", "")
            return {"type": "tool_result", "content": result_text}
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
