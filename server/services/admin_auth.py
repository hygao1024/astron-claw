import hashlib
import secrets

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infra.models import AdminConfig
from infra.log import logger

SESSION_TTL = 86400  # 24 hours
_SESSION_PREFIX = "admin:session:"


class AdminAuth:
    """Admin password auth with MySQL storage and Redis-backed sessions."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Redis,
    ):
        self._session = session_factory
        self._redis = redis

    async def is_password_set(self) -> bool:
        async with self._session() as session:
            row = await session.execute(
                select(AdminConfig.value).where(AdminConfig.key == "password_hash")
            )
            return row.scalar_one_or_none() is not None

    async def set_password(self, password: str) -> None:
        salt = secrets.token_hex(16)
        pw_hash = hashlib.sha256((salt + password).encode()).hexdigest()

        async with self._session() as session:
            # Upsert salt
            result = await session.execute(
                select(AdminConfig).where(AdminConfig.key == "password_salt")
            )
            existing_salt = result.scalar_one_or_none()
            if existing_salt:
                existing_salt.value = salt
            else:
                session.add(AdminConfig(key="password_salt", value=salt))

            # Upsert hash
            result = await session.execute(
                select(AdminConfig).where(AdminConfig.key == "password_hash")
            )
            existing_hash = result.scalar_one_or_none()
            if existing_hash:
                existing_hash.value = pw_hash
            else:
                session.add(AdminConfig(key="password_hash", value=pw_hash))

            await session.commit()
        logger.info("Admin password updated")

    async def verify_password(self, password: str) -> bool:
        async with self._session() as session:
            salt_row = await session.execute(
                select(AdminConfig.value).where(AdminConfig.key == "password_salt")
            )
            hash_row = await session.execute(
                select(AdminConfig.value).where(AdminConfig.key == "password_hash")
            )
            salt = salt_row.scalar_one_or_none()
            stored_hash = hash_row.scalar_one_or_none()

        if not salt or not stored_hash:
            logger.warning("Admin password verification failed: no password configured")
            return False
        expected = hashlib.sha256((salt + password).encode()).hexdigest()
        return secrets.compare_digest(expected, stored_hash)

    async def create_session(self) -> str:
        token = secrets.token_hex(32)
        await self._redis.setex(f"{_SESSION_PREFIX}{token}", SESSION_TTL, "1")
        return token

    async def validate_session(self, session_token: str | None) -> bool:
        if not session_token:
            return False
        result = await self._redis.exists(f"{_SESSION_PREFIX}{session_token}")
        return result > 0

    async def remove_session(self, session_token: str | None) -> None:
        if session_token:
            await self._redis.delete(f"{_SESSION_PREFIX}{session_token}")
