from redis.asyncio import Redis, RedisCluster

from infra.config import RedisConfig
from infra.log import logger

_redis: Redis | RedisCluster | None = None


async def init_redis(config: RedisConfig) -> Redis | RedisCluster:
    """Create and return a Redis client (standalone or cluster)."""
    global _redis

    if config.cluster:
        _redis = RedisCluster(
            host=config.host,
            port=config.port,
            password=config.password or None,
            decode_responses=True,
        )
    else:
        _redis = Redis(
            host=config.host,
            port=config.port,
            password=config.password or None,
            db=config.db,
            decode_responses=True,
        )

    await _redis.ping()
    mode = "cluster" if config.cluster else "standalone"
    logger.info("Redis connected ({}): {}:{}", mode, config.host, config.port)
    return _redis


def get_redis() -> Redis | RedisCluster:
    """Return the global Redis client. Must call init_redis() first."""
    if _redis is None:
        raise RuntimeError("Redis not initialized. Call init_redis() first.")
    return _redis


async def close_redis() -> None:
    """Close the Redis connection."""
    global _redis
    if _redis is not None:
        await _redis.aclose()
        logger.info("Redis connection closed")
    _redis = None
