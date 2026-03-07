"""Tests for services/bridge.py — ConnectionBridge methods (mock Redis + mock Queue)."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from services.bridge import ConnectionBridge


@pytest.fixture()
def bridge(mock_redis, mock_session_store, mock_queue):
    b = ConnectionBridge(mock_redis, session_store=mock_session_store, queue=mock_queue)
    yield b
    # Cancel any consume tasks created during tests
    for task in b._poll_tasks.values():
        task.cancel()


class TestRegisterBot:
    async def test_register_bot_success(self, bridge, mock_redis, mock_queue):
        ws = AsyncMock()
        mock_redis.sismember.return_value = False
        result = await bridge.register_bot("tok-1", ws)
        assert result is True
        assert "tok-1" in bridge._bots
        mock_redis.sadd.assert_awaited()
        mock_queue.ensure_group.assert_awaited_once_with("bridge:bot_inbox:tok-1", "bot")
        # Consume task should be created
        assert "bot:tok-1" in bridge._poll_tasks

    async def test_register_bot_local_dup(self, bridge, mock_redis):
        ws1, ws2 = AsyncMock(), AsyncMock()
        mock_redis.sismember.return_value = False
        await bridge.register_bot("tok-1", ws1)

        result = await bridge.register_bot("tok-1", ws2)
        assert result is False

    async def test_register_bot_redis_dup(self, bridge, mock_redis):
        ws = AsyncMock()
        mock_redis.sismember.return_value = True
        mock_redis.hget.return_value = "other-worker-id"
        mock_redis.exists.return_value = 1  # owning worker is alive
        result = await bridge.register_bot("tok-remote", ws)
        assert result is False


class TestSendToBot:
    async def test_send_to_bot_text(self, bridge, mock_queue, mock_session_store):
        mock_session_store.get_active_session.return_value = "session-id-1"

        req_id = await bridge.send_to_bot("tok-1", "hello", msg_type="text")
        assert req_id is not None
        assert req_id.startswith("req_")
        # pending_requests stores (token, session_id)
        assert bridge._pending_requests[req_id] == ("tok-1", "session-id-1")

        mock_queue.publish.assert_awaited_once()
        inbox_key, payload_str = mock_queue.publish.call_args[0]
        assert inbox_key == "bridge:bot_inbox:tok-1"
        data = json.loads(payload_str)
        sent = data["rpc_request"]
        assert sent["method"] == "session/prompt"
        content_items = sent["params"]["prompt"]["content"]
        assert len(content_items) == 1
        assert content_items[0] == {"type": "text", "text": "hello"}

    async def test_send_to_bot_image(self, bridge, mock_queue, mock_session_store):
        mock_session_store.get_active_session.return_value = "session-id-1"

        media = {
            "mediaId": "media_abc",
            "fileName": "photo.png",
            "mimeType": "image/png",
            "fileSize": 1024,
            "downloadUrl": "/api/media/download/media_abc",
        }
        req_id = await bridge.send_to_bot("tok-1", "my photo", msg_type="image", media=media)
        assert req_id is not None

        inbox_key, payload_str = mock_queue.publish.call_args[0]
        data = json.loads(payload_str)
        sent = data["rpc_request"]
        content_items = sent["params"]["prompt"]["content"]
        assert len(content_items) == 2
        assert content_items[0]["type"] == "text"
        assert content_items[1]["type"] == "media"
        assert content_items[1]["media"]["mediaId"] == "media_abc"

    async def test_send_to_bot_explicit_session_id(self, bridge, mock_queue, mock_session_store):
        """When session_id is passed explicitly, get_active_session is NOT called."""
        req_id = await bridge.send_to_bot("tok-1", "hello", session_id="explicit-sid")
        assert req_id is not None
        assert bridge._pending_requests[req_id] == ("tok-1", "explicit-sid")
        # get_active_session should NOT have been called
        mock_session_store.get_active_session.assert_not_awaited()
        # RPC request should carry the explicit session_id
        _, payload_str = mock_queue.publish.call_args[0]
        data = json.loads(payload_str)
        assert data["rpc_request"]["params"]["sessionId"] == "explicit-sid"


class TestHandleBotMessage:
    async def test_handle_bot_message_invalid_json(self, bridge):
        # Should not raise
        await bridge.handle_bot_message("tok-1", "not json{{{")

    async def test_handle_bot_message_ping(self, bridge):
        # Ping messages should be silently ignored
        await bridge.handle_bot_message("tok-1", json.dumps({"type": "ping"}))

    async def test_routes_chunk_to_session_from_params(self, bridge, mock_queue):
        """Streaming notifications with sessionId in params are routed to that session's inbox."""
        msg = {
            "method": "session/update",
            "params": {
                "sessionId": "session-abc",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"text": "hello"},
                },
            },
        }
        await bridge.handle_bot_message("tok-1", json.dumps(msg))
        mock_queue.publish.assert_awaited_once()
        inbox_key = mock_queue.publish.call_args[0][0]
        assert inbox_key == "bridge:chat_inbox:tok-1:session-abc"
        payload = json.loads(mock_queue.publish.call_args[0][1])
        assert payload["type"] == "chunk"
        assert payload["content"] == "hello"

    async def test_routes_result_to_pending_request_session(self, bridge, mock_queue):
        """JSON-RPC result cleans up pending request but does NOT push a done event.

        The done event is already sent by agent_message_final notification,
        so the result only needs to clean up _pending_requests.
        """
        bridge._pending_requests["req_123"] = ("tok-1", "session-xyz")

        msg = {
            "jsonrpc": "2.0",
            "id": "req_123",
            "result": {"stopReason": "end_turn"},
        }
        await bridge.handle_bot_message("tok-1", json.dumps(msg))
        mock_queue.publish.assert_not_awaited()
        # Pending request should be cleaned up
        assert "req_123" not in bridge._pending_requests

    async def test_routes_error_to_pending_request_session(self, bridge, mock_queue):
        """JSON-RPC error is routed to the session that made the request."""
        bridge._pending_requests["req_456"] = ("tok-1", "session-xyz")

        msg = {
            "jsonrpc": "2.0",
            "id": "req_456",
            "error": {"code": -1, "message": "Bot failed"},
        }
        await bridge.handle_bot_message("tok-1", json.dumps(msg))
        mock_queue.publish.assert_awaited_once()
        inbox_key = mock_queue.publish.call_args[0][0]
        assert inbox_key == "bridge:chat_inbox:tok-1:session-xyz"
        payload = json.loads(mock_queue.publish.call_args[0][1])
        assert payload["type"] == "error"
        assert payload["content"] == "Bot failed"

    async def test_remote_session_gets_inbox_push(self, bridge, mock_queue):
        """JSON-RPC result only cleans up pending request, no inbox push."""
        bridge._pending_requests["req_789"] = ("tok-1", "session-remote")

        msg = {
            "jsonrpc": "2.0",
            "id": "req_789",
            "result": {"stopReason": "end_turn"},
        }
        await bridge.handle_bot_message("tok-1", json.dumps(msg))
        mock_queue.publish.assert_not_awaited()
        assert "req_789" not in bridge._pending_requests


class TestGetConnectionsSummary:
    async def test_get_connections_summary(self, bridge, mock_redis):
        mock_redis.smembers.return_value = {"tok-1", "tok-2"}

        # Pipeline calls: first pipeline returns owners, second returns exists results
        call_count = 0
        def _make_pipeline():
            nonlocal call_count
            call_count += 1
            from unittest.mock import MagicMock, AsyncMock
            pipe = MagicMock()
            if call_count == 1:
                # hget pipeline: returns owner worker_id for each token
                pipe.execute = AsyncMock(return_value=["worker-a", "worker-a"])
            else:
                # exists pipeline: returns 1 (alive) for each unique owner
                pipe.execute = AsyncMock(return_value=[1])
            return pipe
        mock_redis.pipeline = _make_pipeline

        summary = await bridge.get_connections_summary()
        assert summary["tok-1"]["bot_online"] is True
        assert summary["tok-2"]["bot_online"] is True

    async def test_get_connections_summary_empty(self, bridge, mock_redis):
        mock_redis.smembers.return_value = set()
        summary = await bridge.get_connections_summary()
        assert summary == {}


class TestSessionCreateSwitch:
    async def test_create_session(self, bridge, mock_session_store):
        mock_session_store.create_session.return_value = 1
        session_id, number = await bridge.create_session("tok-1")
        assert number == 1
        assert session_id  # non-empty UUID string
        mock_session_store.create_session.assert_awaited_once_with("tok-1", session_id)

    async def test_switch_session_success(self, bridge, mock_session_store):
        mock_session_store.switch_session.return_value = True
        assert await bridge.switch_session("tok-1", "some-session") is True
        mock_session_store.switch_session.assert_awaited_once_with("tok-1", "some-session")

    async def test_switch_session_failure(self, bridge, mock_session_store):
        mock_session_store.switch_session.return_value = False
        assert await bridge.switch_session("tok-1", "nonexistent") is False

    async def test_get_sessions(self, bridge, mock_session_store):
        mock_session_store.get_sessions.return_value = (
            [("sid-1", 1), ("sid-2", 2)], "sid-2"
        )
        sessions, active = await bridge.get_sessions("tok-1")
        assert len(sessions) == 2
        assert sessions[0] == ("sid-1", 1)
        assert active == "sid-2"
        mock_session_store.get_sessions.assert_awaited_once_with("tok-1")

    async def test_get_active_session(self, bridge, mock_session_store):
        mock_session_store.get_active_session.return_value = "sid-1"
        result = await bridge.get_active_session("tok-1")
        assert result == "sid-1"

    async def test_cleanup_old_sessions(self, bridge, mock_session_store):
        mock_session_store.cleanup_old_sessions.return_value = 5
        result = await bridge.cleanup_old_sessions(max_age_days=30)
        assert result == 5
        mock_session_store.cleanup_old_sessions.assert_awaited_once_with(30 * 86400)


# ── Cross-worker inbox tests ─────────────────────────────────────────────────

class TestSendToBotRemote:
    async def test_writes_to_bot_inbox_when_no_local_bot(self, bridge, mock_queue, mock_session_store):
        """When bot is not on this worker, message is pushed to bot_inbox:{token}."""
        mock_session_store.get_active_session.return_value = "session-1"
        # No bot registered locally → remote path
        req_id = await bridge.send_to_bot("tok-1", "hello")
        assert req_id is not None

        mock_queue.publish.assert_awaited_once()
        inbox_key, payload_str = mock_queue.publish.call_args[0]
        assert inbox_key == "bridge:bot_inbox:tok-1"
        data = json.loads(payload_str)
        assert data["rpc_request"]["method"] == "session/prompt"
        assert data["rpc_request"]["params"]["prompt"]["content"][0]["text"] == "hello"


class TestBotStatusNotification:
    async def test_notify_bot_connected_sends_to_active_session(self, bridge, mock_queue, mock_session_store):
        """bot_status is sent to the current active session only."""
        mock_session_store.get_active_session.return_value = "session-active"
        await bridge.notify_bot_connected("tok-1")
        mock_queue.publish.assert_awaited_once()
        inbox_key = mock_queue.publish.call_args[0][0]
        assert inbox_key == "bridge:chat_inbox:tok-1:session-active"
        payload = json.loads(mock_queue.publish.call_args[0][1])
        assert payload == {"type": "bot_status", "connected": True}

    async def test_notify_bot_disconnected_no_active_session(self, bridge, mock_queue, mock_session_store):
        """No push when there is no active session."""
        mock_session_store.get_active_session.return_value = None
        await bridge.notify_bot_disconnected("tok-1")
        mock_queue.publish.assert_not_awaited()


class TestPollBotInbox:
    async def test_forwards_rpc_request_to_bot_ws(self, bridge, mock_queue):
        """_poll_bot_inbox reads one message and forwards rpc_request to bot WS."""
        bot_ws = AsyncMock()
        bridge._bots["tok-1"] = bot_ws  # inject directly, skip register_bot

        rpc_req = {"jsonrpc": "2.0", "id": "req_1", "method": "session/prompt", "params": {}}
        payload = json.dumps({"rpc_request": rpc_req})
        mock_queue.consume.side_effect = [
            ("1-0", payload),
            asyncio.CancelledError(),
        ]

        await bridge._poll_bot_inbox("tok-1")

        bot_ws.send_json.assert_awaited_once_with(rpc_req)
        mock_queue.ack.assert_awaited_once_with("bridge:bot_inbox:tok-1", "bot", "1-0")

    async def test_skips_when_inbox_empty(self, bridge, mock_queue):
        """When inbox is empty, consume returns None and loop continues."""
        bridge._bots["tok-1"] = AsyncMock()
        mock_queue.consume.side_effect = [None, asyncio.CancelledError()]

        await bridge._poll_bot_inbox("tok-1")

        bridge._bots["tok-1"].send_json.assert_not_awaited()
        mock_queue.ack.assert_not_awaited()
