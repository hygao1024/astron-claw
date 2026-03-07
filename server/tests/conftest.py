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
    redis.hget = AsyncMock(return_value=None)
    redis.hset = AsyncMock(return_value=1)
    redis.hgetall = AsyncMock(return_value={})
    redis.hdel = AsyncMock(return_value=1)
    redis.ttl = AsyncMock(return_value=-2)
    redis.expire = AsyncMock(return_value=True)
    redis.ping = AsyncMock(return_value=True)
    # Pipeline mock: returns a chainable object with async execute()
    def _make_pipeline():
        pipe = MagicMock()
        pipe.execute = AsyncMock(return_value=[])
        return pipe
    redis.pipeline = _make_pipeline
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


# ── Mock MessageQueue ────────────────────────────────────────────────────

@pytest.fixture()
def mock_queue():
    """Return an ``AsyncMock`` MessageQueue with sensible defaults."""
    queue = AsyncMock()
    queue.publish = AsyncMock(return_value="1709827200000-0")
    queue.consume = AsyncMock(return_value=None)
    queue.ack = AsyncMock()
    queue.delete_queue = AsyncMock()
    queue.purge = AsyncMock()
    queue.ensure_group = AsyncMock()
    return queue
