"""Tests for services/bridge.py — ConnectionBridge methods (mock Redis)."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.bridge import ConnectionBridge


@pytest.fixture()
def bridge(mock_redis):
    return ConnectionBridge(mock_redis)


class TestRegisterBot:
    async def test_register_bot_success(self, bridge, mock_redis):
        ws = AsyncMock()
        mock_redis.sismember.return_value = False
        result = await bridge.register_bot("tok-1", ws)
        assert result is True
        assert "tok-1" in bridge._bots
        mock_redis.sadd.assert_awaited()

    async def test_register_bot_local_dup(self, bridge, mock_redis):
        ws1, ws2 = AsyncMock(), AsyncMock()
        mock_redis.sismember.return_value = False
        await bridge.register_bot("tok-1", ws1)

        result = await bridge.register_bot("tok-1", ws2)
        assert result is False

    async def test_register_bot_redis_dup(self, bridge, mock_redis):
        ws = AsyncMock()
        mock_redis.sismember.return_value = True
        result = await bridge.register_bot("tok-remote", ws)
        assert result is False


class TestSendToBot:
    async def test_send_to_bot_text(self, bridge, mock_redis):
        ws = AsyncMock()
        mock_redis.sismember.return_value = False
        await bridge.register_bot("tok-1", ws)
        mock_redis.get.return_value = "session-id-1"

        req_id = await bridge.send_to_bot("tok-1", "hello", msg_type="text")
        assert req_id is not None
        assert req_id.startswith("req_")

        sent = ws.send_json.call_args[0][0]
        assert sent["method"] == "session/prompt"
        content_items = sent["params"]["prompt"]["content"]
        assert len(content_items) == 1
        assert content_items[0] == {"type": "text", "text": "hello"}

    async def test_send_to_bot_image(self, bridge, mock_redis):
        ws = AsyncMock()
        mock_redis.sismember.return_value = False
        await bridge.register_bot("tok-1", ws)
        mock_redis.get.return_value = "session-id-1"

        media = {
            "mediaId": "media_abc",
            "fileName": "photo.png",
            "mimeType": "image/png",
            "fileSize": 1024,
            "downloadUrl": "/api/media/download/media_abc",
        }
        req_id = await bridge.send_to_bot("tok-1", "my photo", msg_type="image", media=media)
        assert req_id is not None

        sent = ws.send_json.call_args[0][0]
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


class TestGetConnectionsSummary:
    async def test_get_connections_summary(self, bridge, mock_redis):
        mock_redis.smembers.return_value = {"tok-1", "tok-2"}
        mock_redis.hgetall.return_value = {"tok-1": "3", "tok-3": "1"}

        summary = await bridge.get_connections_summary()
        assert summary["tok-1"]["bot_online"] is True
        assert summary["tok-1"]["chat_count"] == 3
        assert summary["tok-2"]["bot_online"] is True
        assert summary["tok-2"]["chat_count"] == 0
        assert summary["tok-3"]["bot_online"] is False
        assert summary["tok-3"]["chat_count"] == 1


class TestSessionCreateSwitch:
    async def test_session_create_switch(self, bridge, mock_redis):
        # create_session
        mock_redis.llen.return_value = 1
        session_id, number = await bridge.create_session("tok-1")
        assert number == 1
        assert session_id  # non-empty UUID string

        # switch_session with the session in Redis list
        mock_redis.lrange.return_value = [session_id]
        assert await bridge.switch_session("tok-1", session_id) is True

        # switch_session with unknown session
        mock_redis.lrange.return_value = [session_id]
        assert await bridge.switch_session("tok-1", "nonexistent") is False

        # get_sessions
        mock_redis.lrange.return_value = [session_id]
        mock_redis.get.return_value = session_id
        sessions, active = await bridge.get_sessions("tok-1")
        assert len(sessions) == 1
        assert sessions[0][0] == session_id
        assert sessions[0][1] == 1
        assert active == session_id
