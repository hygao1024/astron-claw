import secrets
import time

from sqlalchemy import select, delete, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infra.models import Token
from infra.log import logger

# A far-future timestamp (~year 2200) used for "never expires" tokens.
_NEVER_EXPIRES = 9999999999.0


class TokenManager:
    """MySQL-backed token management with sk- prefix and per-token expiry."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self._session = session_factory

    async def generate(self, name: str = "", expires_in: int = 86400) -> str:
        token_value = "sk-" + secrets.token_hex(24)
        now = time.time()
        expires_at = _NEVER_EXPIRES if expires_in == 0 else now + expires_in

        async with self._session() as session:
            session.add(Token(
                token=token_value,
                created_at=now,
                name=name,
                expires_at=expires_at,
            ))
            await session.commit()

        logger.info("Token generated: {}... (name={}, expires_in={}s)", token_value[:16], name, expires_in)
        return token_value

    async def validate(self, token: str | None) -> bool:
        if not token:
            return False
        async with self._session() as session:
            row = await session.execute(
                select(Token.token).where(
                    Token.token == token,
                    Token.expires_at >= time.time(),
                )
            )
            return row.scalar_one_or_none() is not None

    async def update(
        self, token: str, name: str | None = None, expires_in: int | None = None
    ) -> bool:
        async with self._session() as session:
            row = await session.execute(
                select(Token).where(Token.token == token)
            )
            obj = row.scalar_one_or_none()
            if obj is None:
                logger.warning("Token update failed: {}... not found", token[:16])
                return False
            if name is not None:
                obj.name = name
            if expires_in is not None:
                obj.expires_at = (
                    _NEVER_EXPIRES if expires_in == 0 else time.time() + expires_in
                )
            await session.commit()
        return True

    async def remove(self, token: str) -> None:
        async with self._session() as session:
            await session.execute(
                delete(Token).where(Token.token == token)
            )
            await session.commit()
        logger.info("Token removed: {}...", token[:16])

    async def list_all(self) -> list[dict]:
        now = time.time()
        async with self._session() as session:
            result = await session.execute(
                select(Token).where(Token.expires_at >= now)
            )
            rows = result.scalars().all()
        return [
            {
                "token": row.token,
                "created_at": row.created_at,
                "name": row.name or "",
                "expires_at": row.expires_at,
            }
            for row in rows
        ]

    async def cleanup_expired(self) -> int:
        async with self._session() as session:
            result = await session.execute(
                delete(Token).where(Token.expires_at < time.time())
            )
            await session.commit()
            count = result.rowcount
        if count > 0:
            logger.info("Cleaned up {} expired tokens", count)
        return count
