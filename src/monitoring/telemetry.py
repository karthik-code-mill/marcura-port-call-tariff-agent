"""
OpenTelemetry setup for the Tariff Pipeline.

Current exporter: ConsoleSpanExporter (stdout) — suitable for local development.

TODO(azure-monitor): To ship traces to Azure Application Insights:
  1. pip install azure-monitor-opentelemetry-exporter
  2. Set env var: APPLICATIONINSIGHTS_CONNECTION_STRING=<your connection string>
  3. Replace ConsoleSpanExporter with:
       from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter
       exporter = AzureMonitorTraceExporter(
           connection_string=os.environ["APPLICATIONINSIGHTS_CONNECTION_STRING"]
       )
  4. Optionally add AzureMonitorMetricExporter for metrics.

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
    """
    global _provider
    if _provider is None:
        _provider = TracerProvider()

        # ── Console exporter (default) ────────────────────────────────────────
        console_exporter = ConsoleSpanExporter()
        _provider.add_span_processor(BatchSpanProcessor(console_exporter))

        # TODO(azure-monitor): add Azure Monitor exporter here (see module docstring)

        trace.set_tracer_provider(_provider)

    return trace.get_tracer(service_name)


def get_tracer(service_name: str) -> trace.Tracer:
    """Return (or create) a tracer without re-initialising the provider."""
    return setup_telemetry(service_name)
