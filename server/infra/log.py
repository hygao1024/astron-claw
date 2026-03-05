"""Loguru logging configuration.

Import `logger` from this module instead of using stdlib logging.
Call ``setup_logging()`` once at startup to configure sinks and intercept
third-party libraries that use stdlib logging (uvicorn, sqlalchemy, etc.).
"""

import logging
import sys

from loguru import logger

try:
    from opentelemetry import trace as _otel_trace
except ImportError:  # pragma: no cover — opentelemetry-api is a required dep
    _otel_trace = None


def _get_otel_context() -> str:
    """Extract trace_id and span_id from the current OTel span context.

    Returns a fixed placeholder when OTel is not active or no span exists.
    """
    if _otel_trace is None:
        return "trace_id=- span_id=-"
    try:
        span = _otel_trace.get_current_span()
        ctx = span.get_span_context()
        if ctx and ctx.trace_id:
            return (
                f"trace_id={format(ctx.trace_id, '032x')} "
                f"span_id={format(ctx.span_id, '016x')}"
            )
    except Exception:
        pass
    return "trace_id=- span_id=-"


def _formatter(record: dict) -> str:
    """Dynamic log format string that injects OTel context per-record.

    Using a callable formatter instead of ``logger.patch()`` ensures that
    ALL log records — including stdlib logs intercepted via
    ``_InterceptHandler`` — receive the otel_context field.
    """
    otel_ctx = _get_otel_context()
    return (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
        + otel_ctx
        + " | "
        "<level>{message}</level>\n"
        "{exception}"
    )


def _span_event_sink(message) -> None:
    """Loguru sink that bridges log records to OTel Span Events.

    When a log record is emitted within an active (recording) span, this
    sink calls ``span.add_event()`` so the log message appears in Jaeger's
    span detail view under "Logs".  This is completely non-invasive — no
    business code needs to be modified.

    Only INFO and above are bridged to avoid flooding Jaeger with DEBUG
    noise from polling loops etc.
    """
    if _otel_trace is None:
        return
    try:
        span = _otel_trace.get_current_span()
        if not span or not span.is_recording():
            return

        record = message.record
        level = record["level"].name
        # Skip DEBUG to reduce noise (polling loops, inbox delivery, etc.)
        if record["level"].no < logging.INFO:
            return

        span.add_event(
            name=f"log.{level.lower()}",
            attributes={
                "log.message": record["message"],
                "log.level": level,
                "log.module": record["name"],
                "log.function": record["function"],
                "log.line": record["line"],
            },
        )
    except Exception:
        pass


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
    - Adds a span-event sink that bridges log records into the current
      OTel span so they appear in Jaeger without touching business code.
    - Injects OTel trace_id / span_id into every log line via a dynamic
      formatter function, ensuring both direct ``logger.*()`` calls and
      intercepted stdlib logs are enriched.
    """
    from pathlib import Path

    log_dir = Path(__file__).resolve().parent.parent / "logs"
    log_dir.mkdir(exist_ok=True)

    # Reset loguru sinks
    logger.remove()

    # Console sink (stderr)
    logger.add(
        sys.stderr,
        format=_formatter,
        level=level,
        colorize=True,
    )

    # Rotating file sink
    logger.add(
        str(log_dir / "server.log"),
        format=_formatter,
        level=level,
        rotation="50 MB",
        retention="30 days",
        compression="gz",
        encoding="utf-8",
    )

    # OTel span-event sink (bridges logs → Jaeger span "Logs" tab)
    logger.add(_span_event_sink, level="INFO", format="{message}")

    # Intercept stdlib logging → loguru
    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)

    # Intercept uvicorn loggers — clear their handlers and disable propagation
    # so they only go through the root InterceptHandler once.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers.clear()
        uv_logger.propagate = True
