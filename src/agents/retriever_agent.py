"""
Retriever Agent — Stage 2 §2.3.1
==================================
Autonomous agent. Single public interface: run().

Internal steps (fully encapsulated — not exposed to orchestrator):
  1. Input guardrail   — vessel payload validation
  2. SQL retrieval     — candidate fees from tariff_store (port + GT match)
  3. Holds filter      — skip items blocked by validation_agent via holds config
  4. Chunk context     — supplementary text from chunk_store (skipped if None)
  5. LLM evaluation   — applicability verdict per candidate fee
  6. Assembly          — ApplicableFeeRecord list with formula variables bound

RAG architecture note:
  Current retrieval is VECTORLESS — structured, hierarchical metadata-based
  filtering rather than embedding/similarity search.  The hierarchy is:
    port (exact match) → GT band (range query) → section → fee item
  This is intentional for auditability: every retrieved record is traceable
  to a specific tariff-schedule section and can be explained without an LLM.

TODO(hybrid-search): When the tariff corpus grows beyond a single country or
  when fuzzy port-name matching becomes necessary, add a second retrieval pass
  using vector similarity search (e.g. pgvector, Qdrant, Azure AI Search):
    1. Embed the vessel context (port, vessel_type, cargo_type) with a text
       embedding model (e.g. text-embedding-3-small or Gemini embeddings).
    2. Retrieve top-K semantically similar fee sections as a candidate set.
    3. Merge with the metadata-filter results using a Reciprocal Rank Fusion
       (RRF) or weighted score fusion strategy.
    4. Pass the merged, re-ranked candidates to the LLM applicability evaluator.
  The metadata filter remains as a hard pre-filter (GT band must match) so
  the LLM never sees fee items that are geometrically impossible for the vessel.
"""

import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, SystemMessage
load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))  # project root for guardrails

from models.vessel import VesselInput
from models.retrieval import (
    ApplicabilityResponse,
    ApplicableFeeRecord,
    FeeApplicabilityVerdict,
    RetrieverOutput,
)
from guardrails.retrieval_guardrail import RetrievalGuardrail
from guardrails.llm_output_guardrail import LLMOutputGuardrail, GuardrailViolationError
from guardrails.vessel_input_guardrail import (
    VesselInputGuardrail,
    VesselInputGuardrailError,
)
from monitoring.telemetry import get_tracer
from monitoring.business_metrics import metrics

log = logging.getLogger(__name__)

# Configured via LLM_MODEL env var — e.g. "openai:gpt-4o", "anthropic:claude-sonnet-4-6"
_llm = init_chat_model(
    model=os.getenv("LLM_MODEL", "google_genai:gemini-2.5-flash"),
    temperature=0.0,
)

_ALL_PORTS_LABEL    = "All"
_OTHER_PORTS_LABEL  = "Other"
_retrieval_guardrail = RetrievalGuardrail()
_llm_guardrail       = LLMOutputGuardrail()
_vessel_guardrail    = VesselInputGuardrail()

_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"
_APPLICABILITY_SYSTEM = (_PROMPTS_DIR / "retriever_agent_sp_v3.0.md").read_text(encoding="utf-8")

# Validation holds written by validation_agent — items on hold must not be
# passed to the LLM evaluator even if their DB confidence was not yet zeroed.
_HOLDS_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "context-layer" / "config" / "validation_holds.json"
)

# OpenTelemetry tracer — console exporter by default (see monitoring/telemetry.py).
# TODO(azure-monitor): Switch ConsoleSpanExporter → AzureMonitorTraceExporter
#   in monitoring/telemetry.py and set APPLICATIONINSIGHTS_CONNECTION_STRING env var.
# TODO(azure-insights): Add distributed trace propagation via FastAPI middleware
#   so spans from the API layer, retriever, and calculator appear in one trace.
tracer = get_tracer("tariff.retriever_agent")


# ─── Public Interface ─────────────────────────────────────────────────────────

def run(
    vessel: VesselInput,
    tariff_store,
    chunk_store=None,
) -> RetrieverOutput:
    """
    Retrieve and evaluate applicable fees for the given vessel.
    chunk_store is optional — pipeline continues without chunk context if None.

    Raises VesselInputGuardrailError if the vessel payload fails hard validation.
    """
    with tracer.start_as_current_span("retriever.run") as span:
        span.set_attribute("vessel.port",          vessel.port)
        span.set_attribute("vessel.gross_tonnage", vessel.gross_tonnage)
        span.set_attribute("vessel.voyage_type",   vessel.voyage_type)

        # Step 1 — Input guardrail: validate vessel before any DB work
        guardrail_result = _vessel_guardrail.check(vessel)
        if not guardrail_result:
            metrics.record_guardrail_rejection("vessel_input", len(guardrail_result.violations))
            raise VesselInputGuardrailError(guardrail_result)
        if guardrail_result.warnings:
            for w in guardrail_result.warnings:
                log.info(f"[RetrieverAgent] VesselInputGuardrail warning — {w.field}: {w.message}")

        log.info(f"[RetrieverAgent] port={vessel.port}  GT={vessel.gross_tonnage}  voyage={vessel.voyage_type}")

        # Step 2+3 — SQL retrieval + holds filter
        candidates, held_count = _query_candidate_fees(tariff_store, vessel)
        span.set_attribute("retrieval.candidates", len(candidates))
        span.set_attribute("retrieval.held_count", held_count)

        if not candidates:
            log.warning(f"[RetrieverAgent] No fee records found for port='{vessel.port}' GT={vessel.gross_tonnage}")
            metrics.record_retrieval_miss(vessel.port, vessel.gross_tonnage)
            return RetrieverOutput(vessel=vessel, applicable_fees=[], not_applicable=[], human_review_items=[])

        metrics.record_retrieval_hit(vessel.port, len(candidates), held_count)

        log.info(f"[RetrieverAgent] {len(candidates)} candidates retrieved")
        for i, c in enumerate(candidates, 1):
            log.info(
                f"[RetrieverAgent] candidate {i:02d}  "
                f"section={c.get('section')}  "
                f"port={c.get('port')}  "
                f"gt_range={c.get('vessel_gt_range')}  "
                f"fee_item={c.get('tariff_fee_item')}  "
                f"base_fee={c.get('base_fee')}  "
                f"confidence={c.get('extraction_confidence')}"
            )

        # Step 4 — Chunk context (optional)
        ctx = _fetch_chunk_context(chunk_store, candidates) if chunk_store else ""
        if not ctx:
            log.info("[RetrieverAgent] No chunk context — proceeding with structured data only")

        # Step 5 — LLM applicability evaluation
        response = _evaluate_applicability(vessel, candidates, ctx)
        applicable, not_applicable, review_items = _assemble_applicable_fees(candidates, response.verdicts)

        span.set_attribute("retrieval.applicable",   len(applicable))
        span.set_attribute("retrieval.not_applicable", len(not_applicable))
        span.set_attribute("retrieval.review_items", len(review_items))

        log.info(
            f"[RetrieverAgent] {len(applicable)} applicable  "
            f"{len(not_applicable)} skipped  {len(review_items)} flagged for review"
        )
        return RetrieverOutput(
            vessel=vessel,
            applicable_fees=applicable,
            not_applicable=not_applicable,
            human_review_items=review_items,
        )


# ─── Internal Steps ───────────────────────────────────────────────────────────

def _query_candidate_fees(tariff_store, vessel: VesselInput) -> Tuple[List[dict], int]:
    """
    Vectorless hierarchical metadata retrieval: port + GT-band filter.

    Architecture — this is RAG without vectors:
      Instead of embedding-based similarity, retrieval is a structured SQL
      query keyed on the tariff hierarchy:
        1. port (exact match or wildcard "All" / "Other")
        2. GT band (gt_min ≤ vessel_GT ≤ gt_max range query)
        3. section / fee-item (tie-breaking by confidence and unmodeled_clauses)
      The hierarchy is analogous to a BM25 retrieval filtered by metadata,
      but deterministic and fully explainable without involving an LLM.

    Deduplication strategy:
      Port-specific rows take priority over "Other" rows for the same
      (section, tariff_fee_item) key.  When multiple rows share a key,
      the one with fewer unmodeled_clauses and higher extraction_confidence
      is kept (lower risk of incorrect formula application).

    TODO(hybrid-search): Add a second retrieval pass using vector similarity
      after this metadata filter.  Steps:
        1. Embed vessel context with a text-embedding model.
        2. ANN search against a fee-section embedding index.
        3. Reciprocal Rank Fusion (RRF) to merge ranked lists.
        4. Pass merged top-K candidates to the LLM evaluator.
      The metadata GT-band filter must remain as a hard pre-filter so the
      vector search cannot surface geometrically impossible fee items.

    Returns:
        (candidates, held_count) — candidates ready for LLM evaluation,
        held_count = items dropped by validation holds.
    """
    with tracer.start_as_current_span("retriever.query_candidates") as span:
        span.set_attribute("vessel.port",          vessel.port)
        span.set_attribute("vessel.gross_tonnage", vessel.gross_tonnage)

        gt = vessel.gross_tonnage

        def _fetch(port_label: str) -> list:
            return tariff_store.conn.execute(
                """
                SELECT * FROM tariff_fee_items
                WHERE port = ? AND gt_min <= ? AND gt_max >= ?
                ORDER BY section
                """,
                (port_label, gt, gt),
            ).fetchall()

        # TODO(future-enhancement): Add connection-pool retry logic here once
        #   the tariff store moves from SQLite to a networked DB (PostgreSQL/Azure SQL).
        try:
            port_rows     = _fetch(vessel.port)
            national_rows = _fetch(_ALL_PORTS_LABEL)
            other_rows    = _fetch(_OTHER_PORTS_LABEL)
        except Exception as exc:
            log.error(f"[RetrieverAgent] DB query failed for port='{vessel.port}' GT={vessel.gross_tonnage}: {exc}")
            span.set_attribute("error", str(exc))
            raise  # Surface to pipeline node which records the error and continues

        # Fee items already covered by a port-specific row — 'Other' rows for the
        # same (section, tariff_fee_item) must be excluded to avoid double-counting.
        # 'Other' rows for different fee items (e.g. VTS Charges) are still included.
        port_covered = {(r["section"], r["tariff_fee_item"]) for r in port_rows}

        best: Dict[Tuple[str, str, str], dict] = {}
        for row in port_rows + national_rows + other_rows:
            if row["port"] == _OTHER_PORTS_LABEL and (row["section"], row["tariff_fee_item"]) in port_covered:
                continue
            key = (row["section"], row["tariff_fee_item"], row["vessel_gt_range"])
            d = dict(row)
            existing = best.get(key)
            if existing is None:
                best[key] = d
                continue
            # Prefer the row with fewer unmodeled clauses, then higher confidence.
            existing_unmodeled = len(_parse_json_field(existing.get("unmodeled_clauses"), []))
            new_unmodeled      = len(_parse_json_field(d.get("unmodeled_clauses"), []))
            existing_conf      = existing.get("extraction_confidence") or 0.0
            new_conf           = d.get("extraction_confidence") or 0.0
            if (new_unmodeled, -new_conf) < (existing_unmodeled, -existing_conf):
                best[key] = d

        raw_candidates = list(best.values())

        # Guardrail: reject candidates whose extraction confidence is below threshold.
        passed, rejected = _retrieval_guardrail.filter(raw_candidates)
        if rejected:
            log.info(f"[RetrieverAgent] RetrievalGuardrail rejected {len(rejected)} low-confidence candidates")
            metrics.record_guardrail_rejection("retrieval_confidence", len(rejected))

        # Holds filter: skip items flagged on_hold by validation_agent even if their
        # DB confidence was not yet zeroed (belt-and-suspenders — confidence=0 already
        # causes the RetrievalGuardrail above to drop them, but this makes the
        # hold reason explicit and traceable in logs).
        final, held_count = _filter_validation_holds(passed)
        if held_count:
            metrics.record_guardrail_rejection("validation_holds", held_count)

        span.set_attribute("retrieval.raw_candidates", len(raw_candidates))
        span.set_attribute("retrieval.after_confidence_filter", len(passed))
        span.set_attribute("retrieval.after_holds_filter", len(final))

        return final, held_count


def _filter_validation_holds(candidates: List[dict]) -> Tuple[List[dict], int]:
    """
    Remove candidates that have an active hold in validation_holds.json.

    The holds file is written by validation_agent when it marks an extracted
    record as on_hold.  The key structure is:
        {doc_id: {"{section}__{fee_item}": {reason, timestamp, ...}}}

    Items on hold should not reach the LLM evaluator — their extraction
    fidelity is uncertain and a human must review them first.
    """
    if not _HOLDS_PATH.exists():
        return candidates, 0

    try:
        holds = json.loads(_HOLDS_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning(f"[RetrieverAgent] Could not read validation_holds.json — skipping hold check: {exc}")
        return candidates, 0

    passed: List[dict] = []
    held_count = 0

    for c in candidates:
        doc_id   = c.get("doc_id", "")
        key      = f"{c.get('section')}__{c.get('tariff_fee_item')}"
        doc_holds = holds.get(doc_id, {})

        if key in doc_holds:
            hold_info = doc_holds[key]
            log.warning(
                f"[RetrieverAgent] HOLDS_FILTER  "
                f"section={c.get('section')}  item={c.get('tariff_fee_item')}  "
                f"reason={hold_info.get('reason', '')[:80]}  "
                f"held_since={hold_info.get('timestamp', 'unknown')}"
            )
            held_count += 1
        else:
            passed.append(c)

    if held_count:
        log.info(f"[RetrieverAgent] {held_count} candidate(s) blocked by validation holds")

    return passed, held_count


def _fetch_chunk_context(chunk_store, fees: List[dict]) -> str:
    """Fetch exceptions/conditions/surcharges chunks for each candidate fee."""
    try:
        blocks: List[str] = []
        seen_sections: set = set()
        for fee in fees:
            section  = fee.get("section", "")
            fee_name = fee.get("tariff_fee_item", "")
            if section in seen_sections:
                continue
            seen_sections.add(section)
            for ctype in ("exceptions", "conditions", "surcharges", "notes"):
                chunks = chunk_store.query_by_fee_and_type(fee_name, ctype)
                for c in chunks[:3]:
                    blocks.append(f"[{section} | {fee_name} | {ctype}]\n{c['content']}")
        return "\n\n".join(blocks)
    except Exception as exc:
        log.warning(f"[RetrieverAgent] Chunk context fetch failed (continuing without): {exc}")
        return ""


def _evaluate_applicability(
    vessel: VesselInput,
    candidate_fees: List[dict],
    chunk_context: str,
    max_retries: int = 3,
) -> ApplicabilityResponse:
    vessel_block = (
        f"Port: {vessel.port}\n"
        f"GT: {vessel.gross_tonnage}\n"
        f"Vessel type: {vessel.vessel_type or 'not specified'}\n"
        f"Voyage: {vessel.voyage_type}\n"
        + (f"Cargo type: {vessel.cargo_type}\n" if vessel.cargo_type else "")
        + f"After hours: {vessel.after_hours}\n"
        f"Public holiday: {vessel.public_holiday}\n"
        f"In ballast: {vessel.in_ballast}\n"
        + (f"Special conditions: {', '.join(vessel.special_conditions)}\n"
           if vessel.special_conditions else "")
    )

    fee_summaries = json.dumps([
        {
            "section": f["section"],
            "tariff_fee_item": f["tariff_fee_item"],
            "port": f["port"],
            "vessel_gt_range": f["vessel_gt_range"],
            "conditions": _parse_json_field(f["conditions"], []),
            "surcharges": _parse_json_field(f["surcharges"], []),
            "exceptions": _parse_json_field(f["exceptions"], []),
            "unmodeled_clauses": _parse_json_field(f["unmodeled_clauses"], []),
            "extraction_confidence": f["extraction_confidence"],
        }
        for f in candidate_fees
    ], indent=2)

    prompt = (
        f"VESSEL:\n{vessel_block}\n\n"
        f"CANDIDATE FEES ({len(candidate_fees)}):\n{fee_summaries}\n\n"
        + (f"SUPPLEMENTARY CONTEXT:\n{chunk_context}\n\n" if chunk_context else "")
    )

    messages = [
        SystemMessage(content=_APPLICABILITY_SYSTEM),
        HumanMessage(content=prompt),
    ]
    structured_llm = _llm.with_structured_output(ApplicabilityResponse)

    with tracer.start_as_current_span("retriever.evaluate_applicability") as span:
        span.set_attribute("retriever.candidate_count",  len(candidate_fees))
        span.set_attribute("retriever.has_chunk_context", bool(chunk_context))
        # TODO(azure-monitor): Add span event for each retry attempt so
        #   Application Insights can track LLM reliability per model version.

        for attempt in range(max_retries):
            try:
                raw    = structured_llm.invoke(messages)
                result = _llm_guardrail.validate(raw, ApplicabilityResponse)
                span.set_attribute("retriever.llm_attempts", attempt + 1)
                # TODO(token-metrics): Extract token usage from LLM response metadata
                #   when the LangChain provider exposes it, then call:
                #   metrics.record_token_usage("retriever_agent", prompt_tokens, completion_tokens)
                return result
            except GuardrailViolationError as exc:
                log.error(f"[RetrieverAgent] LLM output guardrail violation: {exc}")
                metrics.record_guardrail_rejection("llm_output", 1)
                if attempt == max_retries - 1:
                    raise
            except Exception as exc:
                if attempt == max_retries - 1:
                    raise
                wait = 2 ** attempt
                log.warning(f"[RetrieverAgent] LLM failed (attempt {attempt + 1}) — retrying in {wait}s: {exc}")
                time.sleep(wait)


def _assemble_applicable_fees(
    candidates: List[dict],
    verdicts: List[FeeApplicabilityVerdict],
) -> Tuple[List[ApplicableFeeRecord], List[dict], List[str]]:
    verdict_map: Dict[Tuple[str, str], FeeApplicabilityVerdict] = {
        (v.section, v.tariff_fee_item): v for v in verdicts
    }
    applicable: List[ApplicableFeeRecord] = []
    not_applicable: List[dict] = []
    review_items: List[str] = []

    for fee in candidates:
        # Guard against malformed DB rows — missing required keys should skip the row,
        # not abort the entire assembly.  A KeyError here would indicate a schema drift
        # between the tariff store and the code.
        # TODO(future-enhancement): Add a DB schema-version check at startup so
        #   column mismatches are detected before any retrieval runs.
        try:
            key = (fee["section"], fee["tariff_fee_item"])
        except KeyError as exc:
            log.error(f"[RetrieverAgent] Malformed DB row missing required key {exc} — skipping row")
            continue

        verdict = verdict_map.get(key)

        if verdict is None or not verdict.applies:
            not_applicable.append(fee)
            continue

        lower, upper = _parse_gt_bounds(fee.get("vessel_gt_range", ""))

        if verdict.needs_human_review:
            review_items.append(fee["tariff_fee_item"])

        applicable.append(ApplicableFeeRecord(
            section=fee["section"],
            tariff_fee_item=fee["tariff_fee_item"],
            port=fee["port"],
            vessel_gt_range=fee["vessel_gt_range"],
            base_fee=fee["base_fee"],
            incremental_fee_per_100_gt=fee["incremental_fee_per_100_gt"],
            formula=fee["formula"],
            conditions=_parse_json_field(fee["conditions"], []),
            surcharges=_parse_json_field(fee["surcharges"], []),
            exceptions=_parse_json_field(fee["exceptions"], []),
            notes=_parse_json_field(fee["notes"], {}),
            unmodeled_clauses=_parse_json_field(fee["unmodeled_clauses"], []),
            extraction_confidence=fee["extraction_confidence"],
            source_page=fee["source_page"],
            applicability_reasoning=verdict.reasoning,
            active_surcharge_conditions=verdict.active_surcharge_conditions,
            needs_human_review=verdict.needs_human_review,
            review_reason=verdict.review_reason,
            gt_lower_bound=lower,
            gt_upper_bound=upper,
        ))

    return applicable, not_applicable, review_items


# ─── Utilities ────────────────────────────────────────────────────────────────

def _parse_json_field(val: Any, default: Any) -> Any:
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return default
    return val if val is not None else default


def _parse_gt_bounds(gt_range: str) -> Tuple[float, float]:
    s = gt_range.strip().lower().replace(",", "").replace(" ", "")
    if not s or s in ("all", "allranges", "allrange", "allvessels"):
        return 0.0, 9_999_999.0
    if "-" in s:
        parts = s.split("-", 1)
        try:
            return float(parts[0]), float(parts[1])
        except ValueError:
            pass
    nums = re.findall(r"[\d.]+", s)
    if nums:
        val = float(nums[0])
        if any(x in s for x in (">", "above", "over", "+")):
            return val, 9_999_999.0
        if any(x in s for x in ("<", "below", "under")):
            return 0.0, val
        return val, val
    return 0.0, 9_999_999.0
