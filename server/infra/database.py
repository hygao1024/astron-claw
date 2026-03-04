from typing import AsyncGenerator
from urllib.parse import quote_plus

import aiomysql
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from infra.config import MysqlConfig
from infra.models import Base
from infra.log import logger

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def _ensure_database(config: MysqlConfig) -> None:
    """Create the database if it does not exist."""
    conn = await aiomysql.connect(
        host=config.host,
        port=config.port,
        user=config.user,
        password=config.password,
    )
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{config.database}` "
                f"DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        logger.info("Ensured database '{}' exists", config.database)
    finally:
        conn.close()


async def init_db(config: MysqlConfig) -> None:
    """Create the async engine and session factory.

    Schema migrations are managed by Alembic — run `alembic upgrade head`
    before first startup or after model changes.
    """
    global _engine, _session_factory

    await _ensure_database(config)

    _engine = create_async_engine(
        config.url,
        pool_size=10,
        max_overflow=5,
        pool_recycle=3600,
        echo=False,
    )

    _session_factory = async_sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    # Verify connectivity
    async with _engine.connect() as conn:
        await conn.execute(__import__("sqlalchemy").text("SELECT 1"))

    logger.info("MySQL connected: {}:{}/{}", config.host, config.port, config.database)


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the global session factory. Must call init_db() first."""
    if _session_factory is None:
        raise RuntimeError("Database not initialized. Call init_db() first.")
    return _session_factory


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Async generator that yields a session and handles cleanup."""
    factory = get_session_factory()
    async with factory() as session:
        yield session


async def close_db() -> None:
    """Dispose the engine and release all connections."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        logger.info("MySQL connection pool closed")
    _engine = None
    _session_factory = None
