"""Tests for services/bridge.py — ConnectionBridge methods (mock Redis)."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.bridge import ConnectionBridge


@pytest.fixture()
def bridge(mock_redis, mock_session_store):
    b = ConnectionBridge(mock_redis, session_store=mock_session_store)
    yield b
    # Cancel any poll tasks created during tests
    for task in b._poll_tasks.values():
        task.cancel()


class TestRegisterBot:
    async def test_register_bot_success(self, bridge, mock_redis):
        ws = AsyncMock()
        mock_redis.sismember.return_value = False
        result = await bridge.register_bot("tok-1", ws)
        assert result is True
        assert "tok-1" in bridge._bots
        mock_redis.sadd.assert_awaited()
        # Poll task should be created
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


class TestRegisterChat:
    async def test_register_chat(self, bridge, mock_redis):
        ws = AsyncMock()
        await bridge.register_chat("tok-1", ws, "session-abc")
        assert ws in bridge._chat_sessions
        assert bridge._chat_sessions[ws] == ("tok-1", "session-abc")
        assert "chat:tok-1:session-abc" in bridge._poll_tasks


class TestSendToBot:
    async def test_send_to_bot_text(self, bridge, mock_redis, mock_session_store):
        mock_session_store.get_active_session.return_value = "session-id-1"

        req_id = await bridge.send_to_bot("tok-1", "hello", msg_type="text")
        assert req_id is not None
        assert req_id.startswith("req_")
        # pending_requests stores (token, session_id, trace_context)
        tok, sid, _tc = bridge._pending_requests[req_id]
        assert (tok, sid) == ("tok-1", "session-id-1")

        mock_redis.rpush.assert_awaited_once()
        inbox_key, payload_str = mock_redis.rpush.call_args[0]
        assert inbox_key == "bridge:bot_inbox:tok-1"
        data = json.loads(payload_str)
        sent = data["rpc_request"]
        assert sent["method"] == "session/prompt"
        content_items = sent["params"]["prompt"]["content"]
        assert len(content_items) == 1
        assert content_items[0] == {"type": "text", "text": "hello"}

    async def test_send_to_bot_image(self, bridge, mock_redis, mock_session_store):
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

        inbox_key, payload_str = mock_redis.rpush.call_args[0]
        data = json.loads(payload_str)
        sent = data["rpc_request"]
        content_items = sent["params"]["prompt"]["content"]
        assert len(content_items) == 2
        assert content_items[0]["type"] == "text"
        assert content_items[1]["type"] == "media"
        assert content_items[1]["media"]["mediaId"] == "media_abc"


class TestHandleBotMessage:
    async def test_handle_bot_message_invalid_json(self, bridge):
        # Should not raise
        await bridge.handle_bot_message("tok-1", "not json{{{")

    async def test_handle_bot_message_ping(self, bridge):
        # Ping messages should be silently ignored
        await bridge.handle_bot_message("tok-1", json.dumps({"type": "ping"}))

    async def test_routes_chunk_to_session_from_params(self, bridge, mock_redis):
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
        mock_redis.rpush.assert_awaited_once()
        inbox_key = mock_redis.rpush.call_args[0][0]
        assert inbox_key == "bridge:chat_inbox:tok-1:session-abc"
        payload = json.loads(mock_redis.rpush.call_args[0][1])
        assert payload["type"] == "chunk"
        assert payload["content"] == "hello"

    async def test_routes_result_to_pending_request_session(self, bridge, mock_redis):
        """JSON-RPC result is routed to the session that made the request."""
        bridge._pending_requests["req_123"] = ("tok-1", "session-xyz", None)

        msg = {
            "jsonrpc": "2.0",
            "id": "req_123",
            "result": {"stopReason": "end_turn"},
        }
        await bridge.handle_bot_message("tok-1", json.dumps(msg))
        mock_redis.rpush.assert_awaited_once()
        inbox_key = mock_redis.rpush.call_args[0][0]
        assert inbox_key == "bridge:chat_inbox:tok-1:session-xyz"
        payload = json.loads(mock_redis.rpush.call_args[0][1])
        assert payload["type"] == "done"
        # Pending request should be cleaned up
        assert "req_123" not in bridge._pending_requests

    async def test_routes_error_to_pending_request_session(self, bridge, mock_redis):
        """JSON-RPC error is routed to the session that made the request."""
        bridge._pending_requests["req_456"] = ("tok-1", "session-xyz", None)

        msg = {
            "jsonrpc": "2.0",
            "id": "req_456",
            "error": {"code": -1, "message": "Bot failed"},
        }
        await bridge.handle_bot_message("tok-1", json.dumps(msg))
        mock_redis.rpush.assert_awaited_once()
        inbox_key = mock_redis.rpush.call_args[0][0]
        assert inbox_key == "bridge:chat_inbox:tok-1:session-xyz"
        payload = json.loads(mock_redis.rpush.call_args[0][1])
        assert payload["type"] == "error"
        assert payload["content"] == "Bot failed"

    async def test_remote_session_gets_inbox_push(self, bridge, mock_redis):
        """When target session is not local, event is pushed to its inbox."""
        # No local chat registered for session-remote
        bridge._pending_requests["req_789"] = ("tok-1", "session-remote", None)

        msg = {
            "jsonrpc": "2.0",
            "id": "req_789",
            "result": {"stopReason": "end_turn"},
        }
        await bridge.handle_bot_message("tok-1", json.dumps(msg))
        mock_redis.rpush.assert_awaited_once()
        inbox_key = mock_redis.rpush.call_args[0][0]
        assert inbox_key == "bridge:chat_inbox:tok-1:session-remote"


class TestGetConnectionsSummary:
    async def test_get_connections_summary(self, bridge, mock_redis):
        # smembers is called twice: first for online_bots, then for workers SET
        mock_redis.smembers.side_effect = [
            {"tok-1", "tok-2"},   # _ONLINE_BOTS_KEY
            {"worker-a"},         # _WORKERS_KEY
        ]
        mock_redis.hget.return_value = "some-worker"
        mock_redis.exists.return_value = 1  # all workers alive
        # hgetall is called once per alive worker for chat counts
        mock_redis.hgetall.return_value = {"tok-1": "3", "tok-3": "1"}

        summary = await bridge.get_connections_summary()
        assert summary["tok-1"]["bot_online"] is True
        assert summary["tok-1"]["chat_count"] == 3
        assert summary["tok-2"]["bot_online"] is True
        assert summary["tok-2"]["chat_count"] == 0
        assert summary["tok-3"]["bot_online"] is False
        assert summary["tok-3"]["chat_count"] == 1


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
    async def test_writes_to_bot_inbox_when_no_local_bot(self, bridge, mock_redis, mock_session_store):
        """When bot is not on this worker, message is pushed to bot_inbox:{token}."""
        mock_session_store.get_active_session.return_value = "session-1"
        # No bot registered locally → remote path
        req_id = await bridge.send_to_bot("tok-1", "hello")
        assert req_id is not None

        mock_redis.rpush.assert_awaited_once()
        inbox_key, payload_str = mock_redis.rpush.call_args[0]
        assert inbox_key == "bridge:bot_inbox:tok-1"
        data = json.loads(payload_str)
        assert data["rpc_request"]["method"] == "session/prompt"
        assert data["rpc_request"]["params"]["prompt"]["content"][0]["text"] == "hello"
        # TTL should be set on the inbox key
        mock_redis.expire.assert_awaited()


class TestBotStatusNotification:
    async def test_notify_bot_connected_sends_to_active_session(self, bridge, mock_redis, mock_session_store):
        """bot_status is sent to the current active session only."""
        mock_session_store.get_active_session.return_value = "session-active"
        await bridge.notify_bot_connected("tok-1")
        mock_redis.rpush.assert_awaited_once()
        inbox_key = mock_redis.rpush.call_args[0][0]
        assert inbox_key == "bridge:chat_inbox:tok-1:session-active"
        payload = json.loads(mock_redis.rpush.call_args[0][1])
        assert payload == {"type": "bot_status", "connected": True}

    async def test_notify_bot_disconnected_no_active_session(self, bridge, mock_redis, mock_session_store):
        """No push when there is no active session."""
        mock_session_store.get_active_session.return_value = None
        await bridge.notify_bot_disconnected("tok-1")
        mock_redis.rpush.assert_not_awaited()


class TestPollBotInbox:
    async def test_forwards_rpc_request_to_bot_ws(self, bridge, mock_redis):
        """_poll_bot_inbox reads one message and forwards rpc_request to bot WS."""
        bot_ws = AsyncMock()
        bridge._bots["tok-1"] = bot_ws  # inject directly, skip register_bot

        rpc_req = {"jsonrpc": "2.0", "id": "req_1", "method": "session/prompt", "params": {}}
        payload = json.dumps({"rpc_request": rpc_req})
        mock_redis.lpop.side_effect = [payload, asyncio.CancelledError()]

        await bridge._poll_bot_inbox("tok-1")

        bot_ws.send_json.assert_awaited_once_with(rpc_req)

    async def test_skips_when_inbox_empty(self, bridge, mock_redis):
        """When inbox is empty, lpop returns None and loop sleeps."""
        bridge._bots["tok-1"] = AsyncMock()
        mock_redis.lpop.side_effect = [None, asyncio.CancelledError()]

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await bridge._poll_bot_inbox("tok-1")

        bridge._bots["tok-1"].send_json.assert_not_awaited()


class TestPollChatInbox:
    async def test_forwards_event_to_chat_ws(self, bridge, mock_redis):
        """_poll_chat_inbox reads one message and sends it to chat WS."""
        chat_ws = AsyncMock()
        payload = json.dumps({"type": "chunk", "content": "hello"})
        mock_redis.lpop.side_effect = [payload, asyncio.CancelledError()]

        await bridge._poll_chat_inbox("tok-1", "session-1", chat_ws)

        chat_ws.send_text.assert_awaited_once_with(payload)

    async def test_skips_when_inbox_empty(self, bridge, mock_redis):
        """When inbox is empty, lpop returns None and loop sleeps."""
        chat_ws = AsyncMock()
        mock_redis.lpop.side_effect = [None, asyncio.CancelledError()]

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await bridge._poll_chat_inbox("tok-1", "session-1", chat_ws)

        chat_ws.send_text.assert_not_awaited()


class TestUpdateChatSession:
    async def test_restarts_poll_task_on_session_change(self, bridge, mock_redis):
        """Switching session stops old poll task, starts new one, cleans old inbox."""
        chat_ws = AsyncMock()
        await bridge.register_chat("tok-1", chat_ws, "session-old")
        assert "chat:tok-1:session-old" in bridge._poll_tasks

        mock_redis.reset_mock()
        await bridge.update_chat_session(chat_ws, "session-new")

        # Old poll task stopped, old inbox deleted
        assert "chat:tok-1:session-old" not in bridge._poll_tasks
        mock_redis.delete.assert_awaited_once_with("bridge:chat_inbox:tok-1:session-old")
        # New poll task started
        assert "chat:tok-1:session-new" in bridge._poll_tasks
        assert bridge._chat_sessions[chat_ws] == ("tok-1", "session-new")

    async def test_noop_when_same_session(self, bridge, mock_redis):
        """No change when session_id is the same."""
        chat_ws = AsyncMock()
        await bridge.register_chat("tok-1", chat_ws, "session-abc")
        mock_redis.reset_mock()

        await bridge.update_chat_session(chat_ws, "session-abc")
        mock_redis.delete.assert_not_awaited()


class TestUnregisterChat:
    async def test_cleans_up_inbox(self, bridge, mock_redis):
        """Unregistering a chat deletes its inbox and cancels poll task."""
        chat_ws = AsyncMock()
        await bridge.register_chat("tok-1", chat_ws, "session-abc")
        mock_redis.reset_mock()

        await bridge.unregister_chat("tok-1", chat_ws)

        mock_redis.delete.assert_awaited_once_with("bridge:chat_inbox:tok-1:session-abc")
        assert chat_ws not in bridge._chat_sessions
        assert "chat:tok-1:session-abc" not in bridge._poll_tasks


# ── Span lifecycle tests ─────────────────────────────────────────────────────


class TestChatRequestSpanLifecycle:
    """Verify the /bridge/chat long-lived span is created, ended, and cleaned up."""

    async def test_send_to_bot_creates_chat_span(self, bridge, mock_redis, mock_session_store):
        """send_to_bot should store a chat request span in _chat_request_spans."""
        mock_session_store.get_active_session.return_value = "session-1"
        await bridge.send_to_bot("tok-1", "hello")
        assert "tok-1:session-1" in bridge._chat_request_spans

    async def test_chat_span_ended_on_done_delivery(self, bridge, mock_redis, mock_session_store):
        """When _poll_chat_inbox delivers a 'done' event, the chat span is ended and removed."""
        mock_session_store.get_active_session.return_value = "session-1"
        await bridge.send_to_bot("tok-1", "hello")
        session_key = "tok-1:session-1"
        assert session_key in bridge._chat_request_spans
        span = bridge._chat_request_spans[session_key]

        # Simulate poll delivering a done event
        done_payload = json.dumps({"type": "done"})
        chat_ws = AsyncMock()
        mock_redis.lpop.side_effect = [done_payload, asyncio.CancelledError()]
        await bridge._poll_chat_inbox("tok-1", "session-1", chat_ws)

        assert session_key not in bridge._chat_request_spans
        chat_ws.send_text.assert_awaited_once_with(done_payload)

    async def test_chat_span_ended_on_error_delivery(self, bridge, mock_redis, mock_session_store):
        """When _poll_chat_inbox delivers an 'error' event, the chat span is ended and removed."""
        mock_session_store.get_active_session.return_value = "session-1"
        await bridge.send_to_bot("tok-1", "hello")
        session_key = "tok-1:session-1"
        assert session_key in bridge._chat_request_spans

        error_payload = json.dumps({"type": "error", "content": "Bot crashed"})
        chat_ws = AsyncMock()
        mock_redis.lpop.side_effect = [error_payload, asyncio.CancelledError()]
        await bridge._poll_chat_inbox("tok-1", "session-1", chat_ws)

        assert session_key not in bridge._chat_request_spans

    async def test_chat_span_superseded_on_consecutive_send(self, bridge, mock_redis, mock_session_store):
        """Consecutive send_to_bot for the same session should end the previous span."""
        mock_session_store.get_active_session.return_value = "session-1"

        await bridge.send_to_bot("tok-1", "first message")
        session_key = "tok-1:session-1"
        first_span = bridge._chat_request_spans[session_key]
        # Wrap end() to track calls; NoOp spans ignore end() so side_effect is safe
        first_span.end = MagicMock()

        mock_redis.reset_mock()
        await bridge.send_to_bot("tok-1", "second message")

        # First span must have been ended before the second one was stored
        first_span.end.assert_called_once()
        # The dict now holds the new (replacement) span
        assert session_key in bridge._chat_request_spans

    async def test_chat_span_cleaned_on_unregister_chat(self, bridge, mock_redis, mock_session_store):
        """unregister_chat should end any active chat request span."""
        mock_session_store.get_active_session.return_value = "session-1"

        chat_ws = AsyncMock()
        await bridge.register_chat("tok-1", chat_ws, "session-1")
        await bridge.send_to_bot("tok-1", "hello")
        session_key = "tok-1:session-1"
        assert session_key in bridge._chat_request_spans

        await bridge.unregister_chat("tok-1", chat_ws)
        assert session_key not in bridge._chat_request_spans

    async def test_chat_span_cleaned_on_shutdown(self, bridge, mock_redis, mock_session_store):
        """shutdown should end all active chat request spans."""
        mock_session_store.get_active_session.return_value = "session-1"
        await bridge.send_to_bot("tok-1", "hello")
        assert len(bridge._chat_request_spans) == 1

        await bridge.shutdown()
        assert len(bridge._chat_request_spans) == 0


class TestResponseSpanLifecycle:
    """Verify the bridge.bot_response long-lived span is created, ended, and cleaned up."""

    async def test_response_span_created_on_first_bot_message(self, bridge, mock_redis):
        """First bot message for a session should create a response span."""
        msg = {
            "method": "session/update",
            "params": {
                "sessionId": "session-abc",
                "update": {"sessionUpdate": "agent_message_chunk", "content": {"text": "hi"}},
            },
        }
        await bridge.handle_bot_message("tok-1", json.dumps(msg))
        assert "tok-1:session-abc" in bridge._response_spans

    async def test_response_span_ended_on_result(self, bridge, mock_redis):
        """JSON-RPC result should end the response span and clean up."""
        bridge._pending_requests["req_1"] = ("tok-1", "session-1", None)
        bridge._active_request_ctx["tok-1:session-1"] = ("req_1", None)

        # First: a chunk creates the response span
        chunk_msg = {
            "method": "session/update",
            "params": {
                "sessionId": "session-1",
                "update": {"sessionUpdate": "agent_message_chunk", "content": {"text": "hello"}},
            },
        }
        await bridge.handle_bot_message("tok-1", json.dumps(chunk_msg))
        assert "tok-1:session-1" in bridge._response_spans

        # Then: result ends it
        mock_redis.reset_mock()
        result_msg = {"jsonrpc": "2.0", "id": "req_1", "result": {"stopReason": "end_turn"}}
        await bridge.handle_bot_message("tok-1", json.dumps(result_msg))
        assert "tok-1:session-1" not in bridge._response_spans
        assert "tok-1:session-1" not in bridge._active_request_ctx

    async def test_response_span_ended_on_error(self, bridge, mock_redis):
        """JSON-RPC error should end the response span and clean up."""
        bridge._pending_requests["req_1"] = ("tok-1", "session-1", None)
        bridge._active_request_ctx["tok-1:session-1"] = ("req_1", None)

        # Create response span via a chunk
        chunk_msg = {
            "method": "session/update",
            "params": {
                "sessionId": "session-1",
                "update": {"sessionUpdate": "agent_message_chunk", "content": {"text": "hi"}},
            },
        }
        await bridge.handle_bot_message("tok-1", json.dumps(chunk_msg))
        assert "tok-1:session-1" in bridge._response_spans

        # Error ends the span
        mock_redis.reset_mock()
        error_msg = {"jsonrpc": "2.0", "id": "req_1", "error": {"code": -1, "message": "fail"}}
        await bridge.handle_bot_message("tok-1", json.dumps(error_msg))
        assert "tok-1:session-1" not in bridge._response_spans

    async def test_response_span_cleaned_on_unregister_bot(self, bridge, mock_redis):
        """unregister_bot should end all response spans for that token."""
        # Inject a response span directly
        from opentelemetry import trace
        span = trace.get_tracer("test").start_span("test-span")
        bridge._response_spans["tok-1:session-1"] = span
        bridge._active_request_ctx["tok-1:session-1"] = ("req_1", None)
        bridge._bots["tok-1"] = AsyncMock()

        mock_redis.hget.return_value = bridge._worker_id
        await bridge.unregister_bot("tok-1")

        assert "tok-1:session-1" not in bridge._response_spans
        assert "tok-1:session-1" not in bridge._active_request_ctx

    async def test_response_span_cleaned_on_shutdown(self, bridge, mock_redis):
        """shutdown should end all active response spans."""
        from opentelemetry import trace
        span = trace.get_tracer("test").start_span("test-span")
        bridge._response_spans["tok-1:session-1"] = span
        bridge._active_request_ctx["tok-1:session-1"] = ("req_1", None)

        await bridge.shutdown()
        assert len(bridge._response_spans) == 0
        assert len(bridge._active_request_ctx) == 0


class TestResultErrorMutualExclusion:
    """Verify that result and error branches in handle_bot_message are mutually exclusive."""

    async def test_message_with_both_result_and_error(self, bridge, mock_redis, mock_session_store):
        """When a malformed message contains both result and error, only result is processed."""
        bridge._pending_requests["req_1"] = ("tok-1", "session-1", None)
        mock_session_store.get_active_session.return_value = "session-1"

        # Malformed msg with both result and error
        msg = {
            "jsonrpc": "2.0",
            "id": "req_1",
            "result": {"stopReason": "end_turn"},
            "error": {"code": -1, "message": "Bot failed"},
        }
        await bridge.handle_bot_message("tok-1", json.dumps(msg))

        # Collect all events pushed to session inbox
        events = []
        for call in mock_redis.rpush.call_args_list:
            inbox_key = call[0][0]
            if inbox_key.startswith("bridge:chat_inbox:"):
                events.append(json.loads(call[0][1]))

        # Only a "done" event should be sent, NOT an "error"
        event_types = [e["type"] for e in events]
        assert "done" in event_types
        assert "error" not in event_types

    async def test_error_only_sends_error(self, bridge, mock_redis, mock_session_store):
        """A pure error message is processed correctly via the elif branch."""
        bridge._pending_requests["req_1"] = ("tok-1", "session-1", None)
        mock_session_store.get_active_session.return_value = "session-1"

        msg = {
            "jsonrpc": "2.0",
            "id": "req_1",
            "error": {"code": -1, "message": "Bot crashed"},
        }
        await bridge.handle_bot_message("tok-1", json.dumps(msg))

        events = []
        for call in mock_redis.rpush.call_args_list:
            inbox_key = call[0][0]
            if inbox_key.startswith("bridge:chat_inbox:"):
                events.append(json.loads(call[0][1]))

        event_types = [e["type"] for e in events]
        assert "error" in event_types
        assert "done" not in event_types
