"""OpenTelemetry SDK initialization.

Provides a single ``setup_telemetry()`` entry-point that configures
TracerProvider and MeterProvider with OTLP/gRPC exporters.  When
``enabled=False`` the function is a no-op and every call to
``get_tracer()`` / ``get_meter()`` returns the built-in NoOp
implementation — zero runtime overhead.
"""

from __future__ import annotations

import asyncio

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource, SERVICE_NAME
from opentelemetry.sdk.trace import SpanLimits, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
    OTLPMetricExporter,
)
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
    OTLPSpanExporter,
)
from starlette.types import ASGIApp, Receive, Scope, Send

_tracer_provider: TracerProvider | None = None
_meter_provider: MeterProvider | None = None


def setup_telemetry(
    enabled: bool,
    service_name: str,
    otlp_endpoint: str,
    metrics_enabled: bool = True,
) -> None:
    """Initialize the OTel SDK and register global providers.

    When *enabled* is ``False`` the function returns immediately and the
    global providers stay as the default NoOp implementations shipped by
    the ``opentelemetry-api`` package.

    When *metrics_enabled* is ``False``, only the TracerProvider is
    configured — no MeterProvider / metric exporter is created.  This
    avoids ``StatusCode.UNIMPLEMENTED`` errors when the OTel Collector
    does not have a metrics pipeline.
    """
    global _tracer_provider, _meter_provider

    if not enabled:
        return

    resource = Resource.create({SERVICE_NAME: service_name})

    # ── Traces ───────────────────────────────────────────────────────
    _tracer_provider = TracerProvider(
        resource=resource,
        span_limits=SpanLimits(max_events=1024),
    )
    _tracer_provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True),
        ),
    )
    trace.set_tracer_provider(_tracer_provider)

    # ── Metrics (optional) ───────────────────────────────────────────
    if metrics_enabled:
        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=otlp_endpoint, insecure=True),
            export_interval_millis=15_000,
        )
        _meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
        metrics.set_meter_provider(_meter_provider)


def shutdown_telemetry() -> None:
    """Flush pending spans/metrics and release exporter resources."""
    if _tracer_provider is not None:
        _tracer_provider.shutdown()
    if _meter_provider is not None:
        _meter_provider.shutdown()


async def shutdown_telemetry_async() -> None:
    """Async wrapper — runs the synchronous gRPC flush in a thread."""
    if _tracer_provider is not None or _meter_provider is not None:
        await asyncio.to_thread(shutdown_telemetry)


def get_tracer(name: str = "astron-claw") -> trace.Tracer:
    """Return a Tracer (NoOp when OTel is disabled)."""
    return trace.get_tracer(name)


def get_meter(name: str = "astron-claw") -> metrics.Meter:
    """Return a Meter (NoOp when OTel is disabled)."""
    return metrics.get_meter(name)


# ── ASGI trace middleware ────────────────────────────────────────────────────


class TraceMiddleware:
    """ASGI middleware that creates a root span for each HTTP request.

    WebSocket connections are traced manually in ``routers/websocket.py``.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.tracer = get_tracer()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET")

        with self.tracer.start_as_current_span(
            f"{method} {path}",
            attributes={
                "http.method": method,
                "http.target": path,
            },
        ) as span:

            async def _send_with_status(message: dict) -> None:
                if message.get("type") == "http.response.start":
                    status = message.get("status", 0)
                    span.set_attribute("http.status_code", status)
                    if status >= 500:
                        span.set_attribute("error", True)
                await send(message)

            await self.app(scope, receive, _send_with_status)
