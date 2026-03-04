"""Tests for services/token_manager.py — TokenManager."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.token_manager import TokenManager


class TestGenerate:
    async def test_generate_format(self, mock_session_factory):
        tm = TokenManager(mock_session_factory)
        token = await tm.generate(name="test")
        assert token.startswith("sk-")
        assert len(token) == 3 + 48  # "sk-" + 24 bytes hex

    async def test_generate_never_expires(self, mock_session_factory):
        tm = TokenManager(mock_session_factory)
        session = mock_session_factory._mock_session

        await tm.generate(name="forever", expires_in=0)

        # Check the Token object passed to session.add
        add_call = session.add.call_args
        token_obj = add_call[0][0]
        assert token_obj.expires_at == 9999999999.0


class TestValidate:
    async def test_validate_none(self, mock_session_factory):
        tm = TokenManager(mock_session_factory)
        assert await tm.validate(None) is False

    async def test_validate_empty(self, mock_session_factory):
        tm = TokenManager(mock_session_factory)
        assert await tm.validate("") is False

    async def test_validate_valid(self, mock_session_factory):
        tm = TokenManager(mock_session_factory)
        session = mock_session_factory._mock_session
        # simulate DB returning a matching row
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = "sk-abc123"
        session.execute.return_value = mock_result

        assert await tm.validate("sk-abc123") is True

    async def test_validate_expired(self, mock_session_factory):
        tm = TokenManager(mock_session_factory)
        session = mock_session_factory._mock_session
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        session.execute.return_value = mock_result

        assert await tm.validate("sk-expired") is False


class TestUpdate:
    async def test_update_not_found(self, mock_session_factory):
        tm = TokenManager(mock_session_factory)
        session = mock_session_factory._mock_session
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        session.execute.return_value = mock_result

        assert await tm.update("sk-missing") is False

    async def test_update_name_only(self, mock_session_factory):
        tm = TokenManager(mock_session_factory)
        session = mock_session_factory._mock_session

        token_obj = MagicMock()
        token_obj.name = "old_name"
        token_obj.expires_at = 9999999999.0
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = token_obj
        session.execute.return_value = mock_result

        result = await tm.update("sk-test", name="new_name")
        assert result is True
        assert token_obj.name == "new_name"
        # expires_at should be unchanged since expires_in was not passed
        assert token_obj.expires_at == 9999999999.0


class TestCleanupExpired:
    async def test_cleanup_expired(self, mock_session_factory):
        tm = TokenManager(mock_session_factory)
        session = mock_session_factory._mock_session
        mock_result = MagicMock()
        mock_result.rowcount = 5
        session.execute.return_value = mock_result

        count = await tm.cleanup_expired()
        assert count == 5
