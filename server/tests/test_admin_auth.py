"""Tests for services/admin_auth.py — AdminAuth."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from services.admin_auth import AdminAuth, _SESSION_PREFIX


class TestValidateSession:
    async def test_validate_session_none(self, mock_session_factory, mock_redis):
        auth = AdminAuth(mock_session_factory, mock_redis)
        assert await auth.validate_session(None) is False

    async def test_validate_session_empty(self, mock_session_factory, mock_redis):
        auth = AdminAuth(mock_session_factory, mock_redis)
        assert await auth.validate_session("") is False

    async def test_validate_session_valid(self, mock_session_factory, mock_redis):
        auth = AdminAuth(mock_session_factory, mock_redis)
        mock_redis.exists.return_value = 1
        assert await auth.validate_session("abc123") is True
        mock_redis.exists.assert_awaited_with(f"{_SESSION_PREFIX}abc123")

    async def test_validate_session_missing(self, mock_session_factory, mock_redis):
        auth = AdminAuth(mock_session_factory, mock_redis)
        mock_redis.exists.return_value = 0
        assert await auth.validate_session("nope") is False


class TestPassword:
    async def test_password_roundtrip(self, mock_session_factory, mock_redis):
        """set_password → verify_password with correct password returns True."""
        auth = AdminAuth(mock_session_factory, mock_redis)
        session = mock_session_factory._mock_session

        # Capture the salt and hash written during set_password
        stored = {}

        def capture_add(obj):
            stored[obj.key] = obj.value

        session.add.side_effect = capture_add

        # set_password: simulate no existing records
        mock_result_none = MagicMock()
        mock_result_none.scalar_one_or_none.return_value = None
        session.execute.return_value = mock_result_none

        await auth.set_password("mypassword")
        assert "password_salt" in stored
        assert "password_hash" in stored

        # verify_password: return the captured salt and hash
        salt = stored["password_salt"]
        pw_hash = stored["password_hash"]

        call_count = 0
        def mock_execute_verify(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:
                result.scalar_one_or_none.return_value = salt
            else:
                result.scalar_one_or_none.return_value = pw_hash
            return result

        session.execute = AsyncMock(side_effect=mock_execute_verify)
        assert await auth.verify_password("mypassword") is True

    async def test_wrong_password(self, mock_session_factory, mock_redis):
        """verify_password with wrong password returns False."""
        auth = AdminAuth(mock_session_factory, mock_redis)
        session = mock_session_factory._mock_session

        stored = {}

        def capture_add(obj):
            stored[obj.key] = obj.value

        session.add.side_effect = capture_add

        mock_result_none = MagicMock()
        mock_result_none.scalar_one_or_none.return_value = None
        session.execute.return_value = mock_result_none

        await auth.set_password("correct")
        salt = stored["password_salt"]
        pw_hash = stored["password_hash"]

        call_count = 0
        def mock_execute_verify(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:
                result.scalar_one_or_none.return_value = salt
            else:
                result.scalar_one_or_none.return_value = pw_hash
            return result

        session.execute = AsyncMock(side_effect=mock_execute_verify)
        assert await auth.verify_password("wrong") is False

    async def test_verify_no_password_set(self, mock_session_factory, mock_redis):
        """verify_password returns False when no password is configured."""
        auth = AdminAuth(mock_session_factory, mock_redis)
        session = mock_session_factory._mock_session
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        session.execute.return_value = mock_result

        assert await auth.verify_password("anything") is False
