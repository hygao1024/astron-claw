"""Tests for routers/sse.py — SSE endpoint handlers (mock state layer)."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from routers.sse import _authenticate, _resolve_session, _sse_event, _sse_comment


# ── Pure helpers ─────────────────────────────────────────────────────────────


class TestSseEvent:
    def test_format(self):
        result = _sse_event("chunk", {"content": "hello"})
        assert result == 'event: chunk\ndata: {"content": "hello"}\n\n'

    def test_unicode(self):
        result = _sse_event("chunk", {"content": "你好"})
        assert "你好" in result
        assert result.startswith("event: chunk\n")

    def test_comment(self):
        assert _sse_comment() == ": heartbeat\n\n"


# ── _authenticate ────────────────────────────────────────────────────────────


class TestAuthenticate:
    async def test_none_header(self):
        assert await _authenticate(None) is None

    async def test_empty_header(self):
        assert await _authenticate("") is None

    async def test_no_bearer_prefix(self):
        assert await _authenticate("Basic abc") is None

    async def test_bearer_empty_token(self):
        assert await _authenticate("Bearer   ") is None

    async def test_valid_token(self):
        with patch("routers.sse.state") as mock_state:
            mock_state.token_manager.validate = AsyncMock(return_value=True)
            result = await _authenticate("Bearer sk-abc123")
            assert result == "sk-abc123"
            mock_state.token_manager.validate.assert_awaited_once_with("sk-abc123")

    async def test_invalid_token(self):
        with patch("routers.sse.state") as mock_state:
            mock_state.token_manager.validate = AsyncMock(return_value=False)
            result = await _authenticate("Bearer sk-expired")
            assert result is None

    async def test_case_insensitive_bearer(self):
        with patch("routers.sse.state") as mock_state:
            mock_state.token_manager.validate = AsyncMock(return_value=True)
            result = await _authenticate("BEARER sk-abc123")
            assert result == "sk-abc123"


# ── _resolve_session ─────────────────────────────────────────────────────────


class TestResolveSession:
    async def test_explicit_session_found(self):
        with patch("routers.sse.state") as mock_state:
            mock_state.bridge.get_sessions = AsyncMock(
                return_value=([("sid-1", 1), ("sid-2", 2)], "sid-1")
            )
            sid, num = await _resolve_session("tok", "sid-2")
            assert sid == "sid-2"
            assert num == 2

    async def test_explicit_session_not_found(self):
        with patch("routers.sse.state") as mock_state:
            mock_state.bridge.get_sessions = AsyncMock(return_value=([], ""))
            with pytest.raises(ValueError, match="Session not found"):
                await _resolve_session("tok", "nonexistent")

    async def test_restore_active_session(self):
        with patch("routers.sse.state") as mock_state:
            mock_state.bridge.get_active_session = AsyncMock(return_value="sid-1")
            mock_state.bridge.get_sessions = AsyncMock(
                return_value=([("sid-1", 1)], "sid-1")
            )
            sid, num = await _resolve_session("tok", None)
            assert sid == "sid-1"
            assert num == 1

    async def test_active_session_stale_creates_new(self):
        """Active session ID exists in Redis but not in session list — create new."""
        with patch("routers.sse.state") as mock_state:
            mock_state.bridge.get_active_session = AsyncMock(return_value="stale-id")
            mock_state.bridge.get_sessions = AsyncMock(return_value=([], ""))
            mock_state.bridge.create_session = AsyncMock(return_value=("new-id", 1))
            sid, num = await _resolve_session("tok", None)
            assert sid == "new-id"
            mock_state.bridge.create_session.assert_awaited_once_with("tok")

    async def test_no_active_creates_new(self):
        with patch("routers.sse.state") as mock_state:
            mock_state.bridge.get_active_session = AsyncMock(return_value=None)
            mock_state.bridge.create_session = AsyncMock(return_value=("new-id", 1))
            sid, num = await _resolve_session("tok", None)
            assert sid == "new-id"
            assert num == 1


# ── Endpoint integration (via FastAPI TestClient) ────────────────────────────

# We test the route handlers by importing the router and calling them
# with mocked state, avoiding the need for a full app setup.


class TestChatSseEndpoint:
    """Test chat_sse route handler validation paths."""

    async def test_401_no_auth(self):
        with patch("routers.sse.state") as mock_state:
            mock_state.token_manager.validate = AsyncMock(return_value=False)
            from routers.sse import chat_sse, ChatRequest
            body = ChatRequest(content="hello")
            resp = await chat_sse(body, authorization=None)
            assert resp.status_code == 401

    async def test_401_invalid_token(self):
        with patch("routers.sse.state") as mock_state:
            mock_state.token_manager.validate = AsyncMock(return_value=False)
            from routers.sse import chat_sse, ChatRequest
            body = ChatRequest(content="hello")
            resp = await chat_sse(body, authorization="Bearer sk-bad")
            assert resp.status_code == 401

    async def test_400_empty_text(self):
        with patch("routers.sse.state") as mock_state:
            mock_state.token_manager.validate = AsyncMock(return_value=True)
            from routers.sse import chat_sse, ChatRequest
            body = ChatRequest(content="")
            resp = await chat_sse(body, authorization="Bearer sk-valid")
            assert resp.status_code == 400
            assert "Empty message" in resp.body.decode()

    async def test_400_media_missing(self):
        with patch("routers.sse.state") as mock_state:
            mock_state.token_manager.validate = AsyncMock(return_value=True)
            from routers.sse import chat_sse, ChatRequest
            body = ChatRequest(content="", msgType="image", media=None)
            resp = await chat_sse(body, authorization="Bearer sk-valid")
            assert resp.status_code == 400
            assert "Missing media info" in resp.body.decode()

    async def test_400_bot_not_connected(self):
        with patch("routers.sse.state") as mock_state:
            mock_state.token_manager.validate = AsyncMock(return_value=True)
            mock_state.bridge.is_bot_connected = AsyncMock(return_value=False)
            from routers.sse import chat_sse, ChatRequest
            body = ChatRequest(content="hello")
            resp = await chat_sse(body, authorization="Bearer sk-valid")
            assert resp.status_code == 400
            assert "No bot connected" in resp.body.decode()

    async def test_404_session_not_found(self):
        with patch("routers.sse.state") as mock_state:
            mock_state.token_manager.validate = AsyncMock(return_value=True)
            mock_state.bridge.is_bot_connected = AsyncMock(return_value=True)
            mock_state.bridge.get_sessions = AsyncMock(return_value=([], ""))
            from routers.sse import chat_sse, ChatRequest
            body = ChatRequest(content="hello", sessionId="nonexistent")
            resp = await chat_sse(body, authorization="Bearer sk-valid")
            assert resp.status_code == 404
            assert "Session not found" in resp.body.decode()

    async def test_500_send_to_bot_fails(self):
        with patch("routers.sse.state") as mock_state, \
             patch("routers.sse.get_redis") as mock_get_redis:
            mock_state.token_manager.validate = AsyncMock(return_value=True)
            mock_state.bridge.is_bot_connected = AsyncMock(return_value=True)
            mock_state.bridge.get_sessions = AsyncMock(
                return_value=([("sid-1", 1)], "sid-1")
            )
            mock_state.bridge.switch_session = AsyncMock(return_value=True)
            mock_state.bridge.send_to_bot = AsyncMock(return_value=None)
            mock_redis = AsyncMock()
            mock_get_redis.return_value = mock_redis
            from routers.sse import chat_sse, ChatRequest
            body = ChatRequest(content="hello", sessionId="sid-1")
            resp = await chat_sse(body, authorization="Bearer sk-valid")
            assert resp.status_code == 500
            assert "Failed to send" in resp.body.decode()

    async def test_200_returns_sse_stream(self):
        with patch("routers.sse.state") as mock_state, \
             patch("routers.sse.get_redis") as mock_get_redis:
            mock_state.token_manager.validate = AsyncMock(return_value=True)
            mock_state.bridge.is_bot_connected = AsyncMock(return_value=True)
            mock_state.bridge.get_sessions = AsyncMock(
                return_value=([("sid-1", 1)], "sid-1")
            )
            mock_state.bridge.switch_session = AsyncMock(return_value=True)
            mock_state.bridge.send_to_bot = AsyncMock(return_value="req_abc")
            mock_redis = AsyncMock()
            mock_get_redis.return_value = mock_redis
            from routers.sse import chat_sse, ChatRequest
            body = ChatRequest(content="hello", sessionId="sid-1")
            resp = await chat_sse(body, authorization="Bearer sk-valid")
            assert resp.media_type == "text/event-stream"
            assert resp.headers["Cache-Control"] == "no-cache"

    async def test_inbox_cleared_before_send(self):
        """Stale events in inbox are deleted before sending to bot."""
        with patch("routers.sse.state") as mock_state, \
             patch("routers.sse.get_redis") as mock_get_redis:
            mock_state.token_manager.validate = AsyncMock(return_value=True)
            mock_state.bridge.is_bot_connected = AsyncMock(return_value=True)
            mock_state.bridge.get_sessions = AsyncMock(
                return_value=([("sid-1", 1)], "sid-1")
            )
            mock_state.bridge.switch_session = AsyncMock(return_value=True)
            mock_state.bridge.send_to_bot = AsyncMock(return_value="req_abc")
            mock_redis = AsyncMock()
            mock_get_redis.return_value = mock_redis
            from routers.sse import chat_sse, ChatRequest
            body = ChatRequest(content="hello", sessionId="sid-1")
            await chat_sse(body, authorization="Bearer sk-valid")
            # Verify inbox was deleted for this specific session
            mock_redis.delete.assert_awaited_once_with(
                "bridge:chat_inbox:sk-valid:sid-1"
            )


class TestListSessionsEndpoint:
    async def test_401(self):
        with patch("routers.sse.state") as mock_state:
            mock_state.token_manager.validate = AsyncMock(return_value=False)
            from routers.sse import list_sessions
            resp = await list_sessions(authorization=None)
            assert resp.status_code == 401

    async def test_200(self):
        with patch("routers.sse.state") as mock_state:
            mock_state.token_manager.validate = AsyncMock(return_value=True)
            mock_state.bridge.get_sessions = AsyncMock(
                return_value=([("sid-1", 1), ("sid-2", 2)], "sid-2")
            )
            from routers.sse import list_sessions
            resp = await list_sessions(authorization="Bearer sk-valid")
            assert resp["ok"] is True
            assert len(resp["sessions"]) == 2
            assert resp["activeSessionId"] == "sid-2"


class TestCreateSessionEndpoint:
    async def test_401(self):
        with patch("routers.sse.state") as mock_state:
            mock_state.token_manager.validate = AsyncMock(return_value=False)
            from routers.sse import create_session
            resp = await create_session(authorization=None)
            assert resp.status_code == 401

    async def test_200(self):
        with patch("routers.sse.state") as mock_state:
            mock_state.token_manager.validate = AsyncMock(return_value=True)
            mock_state.bridge.create_session = AsyncMock(return_value=("sid-new", 3))
            mock_state.bridge.get_sessions = AsyncMock(
                return_value=([("sid-1", 1), ("sid-new", 3)], "sid-new")
            )
            from routers.sse import create_session
            resp = await create_session(authorization="Bearer sk-valid")
            assert resp["ok"] is True
            assert resp["sessionId"] == "sid-new"
            assert resp["sessionNumber"] == 3
            assert len(resp["sessions"]) == 2
