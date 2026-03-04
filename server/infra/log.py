"""Loguru logging configuration.

Import `logger` from this module instead of using stdlib logging.
Call `setup_logging()` once at startup to configure sinks and intercept
third-party libraries that use stdlib logging (uvicorn, sqlalchemy, etc.).
"""

import logging
import sys

from loguru import logger

LOG_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)


class _InterceptHandler(logging.Handler):
    """Redirect stdlib logging records to loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        # Map stdlib level to loguru level
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Find the caller that originated the log call
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def setup_logging(level: str = "INFO") -> None:
    """Configure loguru as the sole logging backend.

    - Removes default loguru sink and adds a formatted stderr sink.
    - Intercepts stdlib logging so uvicorn / sqlalchemy / alembic logs
      are also routed through loguru.
    - Adds a rotating file sink at ``server/logs/server.log``.
    """
    from pathlib import Path

    log_dir = Path(__file__).resolve().parent.parent / "logs"
    log_dir.mkdir(exist_ok=True)

    # Reset loguru sinks
    logger.remove()

    # Console sink (stderr)
    logger.add(
        sys.stderr,
        format=LOG_FORMAT,
        level=level,
        colorize=True,
    )

    # Rotating file sink
    logger.add(
        str(log_dir / "server.log"),
        format=LOG_FORMAT,
        level=level,
        rotation="50 MB",
        retention="30 days",
        compression="gz",
        encoding="utf-8",
    )

    # Intercept stdlib logging → loguru
    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)

    # Intercept uvicorn loggers — clear their handlers and disable propagation
    # so they only go through the root InterceptHandler once.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers.clear()
        uv_logger.propagate = True
