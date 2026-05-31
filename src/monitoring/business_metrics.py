"""
Business Metrics Collector — pipeline-level KPI tracking.

Current implementation: in-process counters + structured log lines.
All metrics are emitted as INFO log records with a consistent "[metrics]"
prefix so they can be filtered and parsed by any log aggregation tool
(Azure Log Analytics, Splunk, Datadog, etc.).

State resets per process lifetime — not persisted between runs.

TODO(production-metrics): Promote to production-grade by choosing one of:
  Option A — Azure Monitor (Application Insights):
    pip install azure-monitor-opentelemetry
    Use AzureMonitorMetricExporter from azure.monitor.opentelemetry.exporter.
    Custom metrics via opentelemetry.metrics.Counter / Histogram.
    Correlation with traces via W3C TraceContext propagation.

  Option B — Prometheus + Grafana:
    pip install prometheus_client
    Expose /metrics HTTP endpoint in FastAPI.
    Counters: retrieval_misses_total, calculation_errors_total, guardrail_rejections_total.
    Histograms: fee_relative_error_ratio, token_usage_tokens.
    Dashboards: Grafana with fee-accuracy, retrieval-hit-rate, cost-per-request panels.

  Option C — OpenTelemetry Metrics API (vendor-neutral):
    from opentelemetry import metrics as otel_metrics
    meter = otel_metrics.get_meter("tariff.pipeline")
    Use Counters and Histograms; plug in any exporter at runtime.

In all cases, add per-request tenant/vessel context (port, country, version_tag)
as metric labels/dimensions for sliceability.

TODO(production-metrics): Add alerting rules for:
  - retrieval_miss_rate > 5% of requests in 5-min window
  - calculation_error_rate > 1% of line items
  - fee_accuracy_pct < 95% over rolling 100-sample window
  - p95 token usage > 30k tokens/call (prompt compression trigger)
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from threading import Lock
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


@dataclass
class FeeAccuracySample:
    section: str
    fee_item: str
    expected: float
    computed: float
    relative_error: float
    accurate: bool


class BusinessMetricsCollector:
    """
    Collect and log business-level pipeline metrics.

    Thread-safe: a single lock protects all counter updates so this instance
    can be shared across concurrent pipeline runs in an async FastAPI server.

    Usage:
        from monitoring.business_metrics import metrics

        metrics.record_retrieval_miss(vessel.port, vessel.gross_tonnage)
        metrics.record_token_usage("retriever_agent", prompt_tokens=1200, completion_tokens=300)
        metrics.log_snapshot()
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._retrieval_misses:     int = 0
        self._retrieval_hits:       int = 0
        self._held_items:           int = 0
        self._calculation_errors:   int = 0
        self._guardrail_rejections: Dict[str, int] = defaultdict(int)
        self._token_totals:         Dict[str, int] = defaultdict(int)
        self._accuracy_samples:     List[FeeAccuracySample] = []

    # ── Retrieval metrics ─────────────────────────────────────────────────────

    def record_retrieval_miss(self, port: str, gross_tonnage: float) -> None:
        """No fee candidates found for this port+GT combination."""
        with self._lock:
            self._retrieval_misses += 1
        log.info(
            f"[metrics] retrieval_miss  port={port}  gt={gross_tonnage}  "
            f"total_misses={self._retrieval_misses}"
        )
        # TODO(production-metrics): push counter to Azure Monitor / Prometheus

    def record_retrieval_hit(self, port: str, candidate_count: int, held_count: int = 0) -> None:
        """Candidates were found; held_count were blocked by validation holds."""
        with self._lock:
            self._retrieval_hits += 1
            self._held_items += held_count
        log.info(
            f"[metrics] retrieval_hit  port={port}  "
            f"candidates={candidate_count}  held={held_count}"
        )

    # ── Calculation metrics ───────────────────────────────────────────────────

    def record_calculation_error(self, section: str, fee_item: str, error_msg: str) -> None:
        """Formula evaluation failed for this (section, fee_item) pair."""
        with self._lock:
            self._calculation_errors += 1
        log.info(
            f"[metrics] calculation_error  section={section}  item={fee_item}  "
            f"error={error_msg[:100]}  total_errors={self._calculation_errors}"
        )
        # TODO(production-metrics): emit as Application Insights custom event with
        # properties: {"section": section, "fee_item": fee_item, "error": error_msg}

    def record_fee_accuracy(
        self,
        section: str,
        fee_item: str,
        expected_amount: float,
        computed_amount: float,
        accuracy_tolerance: float = 0.01,
    ) -> None:
        """
        Record a fee accuracy data point against a known-good benchmark amount.

        accuracy_tolerance: relative error threshold below which a result is
        considered accurate (default 1%).

        TODO(production-metrics): In production, benchmark amounts come from a
        validated golden dataset (e.g. audited invoices per port/version).
        Hook this into an automated regression suite that runs after each
        tariff DB update to catch extraction regressions early.
        """
        relative_error = abs(computed_amount - expected_amount) / max(abs(expected_amount), 1.0)
        accurate = relative_error <= accuracy_tolerance
        sample = FeeAccuracySample(
            section=section,
            fee_item=fee_item,
            expected=expected_amount,
            computed=computed_amount,
            relative_error=relative_error,
            accurate=accurate,
        )
        with self._lock:
            self._accuracy_samples.append(sample)
        log.info(
            f"[metrics] fee_accuracy  section={section}  item={fee_item}  "
            f"expected={expected_amount:.2f}  computed={computed_amount:.2f}  "
            f"relative_err={relative_error:.4f}  accurate={accurate}"
        )

    # ── Guardrail metrics ─────────────────────────────────────────────────────

    def record_guardrail_rejection(self, guardrail_name: str, count: int) -> None:
        """Track how many records each guardrail rejected in this run."""
        if count <= 0:
            return
        with self._lock:
            self._guardrail_rejections[guardrail_name] += count
        log.info(
            f"[metrics] guardrail_rejection  guardrail={guardrail_name}  "
            f"count={count}  cumulative={self._guardrail_rejections[guardrail_name]}"
        )
        # TODO(production-metrics): track per-guardrail rejection rate as a Prometheus
        # gauge so ops can detect tariff-DB quality degradation early.

    # ── Token-usage metrics ───────────────────────────────────────────────────

    def record_token_usage(
        self,
        agent_name: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> None:
        """
        Record LLM token consumption per agent call.

        Token usage is the primary cost driver.  In production:
          - Track cumulative usage per request for per-voyage cost attribution.
          - Alert when p95 prompt_tokens/call exceeds a threshold; that signals
            the fee-summary payload has grown too large and needs compression.
          - Use this data to decide when prompt caching (Anthropic / Google AI)
            pays off for the system prompt.

        TODO(production-metrics): Push to Azure Cost Management via custom metric
        or store in a billing table keyed by (request_id, agent_name, timestamp).
        """
        total = prompt_tokens + completion_tokens
        with self._lock:
            self._token_totals[agent_name] += total
        log.info(
            f"[metrics] token_usage  agent={agent_name}  "
            f"prompt={prompt_tokens}  completion={completion_tokens}  total={total}  "
            f"agent_cumulative={self._token_totals[agent_name]}"
        )

    # ── Snapshot ──────────────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """Point-in-time summary of all accumulated metrics for this process."""
        with self._lock:
            n_samples = len(self._accuracy_samples)
            accurate_n = sum(1 for s in self._accuracy_samples if s.accurate)
            accuracy_pct = (accurate_n / n_samples * 100) if n_samples else None
            return {
                "retrieval_hits":       self._retrieval_hits,
                "retrieval_misses":     self._retrieval_misses,
                "held_items":           self._held_items,
                "calculation_errors":   self._calculation_errors,
                "guardrail_rejections": dict(self._guardrail_rejections),
                "token_totals":         dict(self._token_totals),
                "fee_accuracy_pct":     round(accuracy_pct, 2) if accuracy_pct is not None else None,
                "fee_accuracy_samples": n_samples,
            }

    def log_snapshot(self) -> None:
        """Emit a single structured INFO line with the full metrics snapshot.
        Call at pipeline end or on a periodic schedule."""
        snap = self.snapshot()
        log.info(f"[metrics] snapshot  {snap}")
        # TODO(production-metrics): push snapshot to Azure Application Insights
        # as a custom event named "PipelineMetricsSnapshot" with snap as properties.


# Module-level singleton — import directly in agent modules.
#
# TODO(production-metrics): Replace with a dependency-injected instance (via
# FastAPI Depends or a request-scoped context var) so metrics are isolated
# per request in a multi-tenant deployment and can carry per-request labels
# (request_id, user_id, vessel_port, tariff_version).
metrics = BusinessMetricsCollector()
