"""Shared fixtures for unit tests."""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Ensure `server/` is on sys.path so `from infra.xxx` imports work.
_server_dir = Path(__file__).resolve().parent.parent
if str(_server_dir) not in sys.path:
    sys.path.insert(0, str(_server_dir))


# ── Mock async_sessionmaker ──────────────────────────────────────────────────

@pytest.fixture()
def mock_session_factory():
    """Return a mock ``async_sessionmaker`` whose context manager yields an
    ``AsyncSession`` stub with controllable ``execute()`` results."""
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.execute = AsyncMock()

    factory = MagicMock()
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    factory.return_value = ctx

    # Attach raw session for test convenience
    factory._mock_session = session
    return factory


# ── Mock Redis ───────────────────────────────────────────────────────────────

@pytest.fixture()
def mock_redis():
    """Return an ``AsyncMock`` Redis client with sensible defaults."""
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=True)
    redis.setex = AsyncMock(return_value=True)
    redis.exists = AsyncMock(return_value=0)
    redis.delete = AsyncMock(return_value=1)
    redis.sismember = AsyncMock(return_value=False)
    redis.sadd = AsyncMock(return_value=1)
    redis.srem = AsyncMock(return_value=1)
    redis.smembers = AsyncMock(return_value=set())
    redis.hincrby = AsyncMock(return_value=1)
    redis.hgetall = AsyncMock(return_value={})
    redis.hdel = AsyncMock(return_value=1)
    redis.rpush = AsyncMock(return_value=1)
    redis.lrange = AsyncMock(return_value=[])
    redis.llen = AsyncMock(return_value=0)
    redis.ttl = AsyncMock(return_value=-2)
    redis.expire = AsyncMock(return_value=True)
    redis.ping = AsyncMock(return_value=True)
    redis.lpop = AsyncMock(return_value=None)
    return redis


# ── Mock SessionStore ─────────────────────────────────────────────────────

@pytest.fixture()
def mock_session_store():
    """Return an ``AsyncMock`` SessionStore with sensible defaults."""
    store = AsyncMock()
    store.create_session = AsyncMock(return_value=1)
    store.get_active_session = AsyncMock(return_value=None)
    store.get_sessions = AsyncMock(return_value=([], ""))
    store.switch_session = AsyncMock(return_value=True)
    store.remove_sessions = AsyncMock()
    store.cleanup_old_sessions = AsyncMock(return_value=0)
    return store
