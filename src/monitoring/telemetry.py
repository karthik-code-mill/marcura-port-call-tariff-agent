"""
OpenTelemetry setup for the Tariff Pipeline.

Default: no-op (spans are recorded but not exported).
Set OTEL_TRACE_CONSOLE=1 to print JSON spans to stdout for local debugging.

TODO(azure-monitor): To ship traces to Azure Application Insights:
  1. pip install azure-monitor-opentelemetry-exporter
  2. Set env var: APPLICATIONINSIGHTS_CONNECTION_STRING=<your connection string>
  3. Add AzureMonitorTraceExporter inside setup_telemetry (see comment below).

TODO(azure-insights): For distributed tracing across services (e.g. API → pipeline → agents):
  - Add OTLP exporter alongside Azure Monitor so local Jaeger/Zipkin can also receive spans.
  - Propagate W3C TraceContext headers in FastAPI middleware.
"""

import os
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

_provider: TracerProvider | None = None


def setup_telemetry(service_name: str) -> trace.Tracer:
    """
    Initialise the global TracerProvider (idempotent — safe to call multiple times).
    Returns a named tracer for the given service.

    Console JSON export is OFF by default — set OTEL_TRACE_CONSOLE=1 to enable.
    """
    global _provider
    if _provider is None:
        _provider = TracerProvider()

        # ── Console exporter — opt-in only, avoids flooding tqdm output ───────
        if os.getenv("OTEL_TRACE_CONSOLE", "").strip() == "1":
            _provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

        # TODO(azure-monitor): add Azure Monitor exporter here (see module docstring)

        trace.set_tracer_provider(_provider)

    return trace.get_tracer(service_name)


def get_tracer(service_name: str) -> trace.Tracer:
    """Return (or create) a tracer without re-initialising the provider."""
    return setup_telemetry(service_name)
