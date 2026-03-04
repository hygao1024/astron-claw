from fastapi import APIRouter

from infra.log import logger

router = APIRouter()


@router.get("/api/health")
async def health_check():
    mysql_ok = False
    redis_ok = False

    try:
        from infra.database import _engine
        if _engine is not None:
            async with _engine.connect() as conn:
                await conn.execute(__import__("sqlalchemy").text("SELECT 1"))
            mysql_ok = True
    except Exception as e:
        logger.warning("MySQL health check failed: {}", str(e))

    try:
        from infra.cache import get_redis
        r = get_redis()
        await r.ping()
        redis_ok = True
    except Exception as e:
        logger.warning("Redis health check failed: {}", str(e))

    status = "ok" if (mysql_ok and redis_ok) else "degraded"
    if status == "degraded":
        logger.warning("Health check degraded — MySQL={}, Redis={}", mysql_ok, redis_ok)
    return {"status": status, "mysql": mysql_ok, "redis": redis_ok}
