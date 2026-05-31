"""
Retriever Agent — Stage 2 §2.3.1
==================================
Autonomous agent. Single public interface: run().

Internal steps (fully encapsulated — not exposed to orchestrator):
  1. SQL retrieval  — candidate fees from tariff_store (port + GT match)
  2. Chunk context  — supplementary text from chunk_store (skipped if None)
  3. LLM evaluation — applicability verdict per candidate fee
  4. Assembly       — ApplicableFeeRecord list with formula variables bound
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

from models.vessel import VesselInput
from models.retrieval import (
    ApplicabilityResponse,
    ApplicableFeeRecord,
    FeeApplicabilityVerdict,
    RetrieverOutput,
)

log = logging.getLogger(__name__)

# Configured via LLM_MODEL env var — e.g. "openai:gpt-4o", "anthropic:claude-sonnet-4-6"
_llm = init_chat_model(
    model=os.getenv("LLM_MODEL", "google_genai:gemini-2.5-flash"),
    temperature=0.0,
)

_ALL_PORTS_LABEL   = "All"
_OTHER_PORTS_LABEL = "Other"

_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"
_APPLICABILITY_SYSTEM = (_PROMPTS_DIR / "retriever_agent_sp_v3.0.md").read_text(encoding="utf-8")


# ─── Public Interface ─────────────────────────────────────────────────────────

def run(
    vessel: VesselInput,
    tariff_store,
    chunk_store=None,
) -> RetrieverOutput:
    """
    Retrieve and evaluate applicable fees for the given vessel.
    chunk_store is optional — pipeline continues without chunk context if None.
    """
    log.info(f"[RetrieverAgent] port={vessel.port}  GT={vessel.gross_tonnage}  voyage={vessel.voyage_type}")

    candidates = _query_candidate_fees(tariff_store, vessel)
    if not candidates:
        log.warning(f"[RetrieverAgent] No fee records found for port='{vessel.port}' GT={vessel.gross_tonnage}")
        return RetrieverOutput(vessel=vessel, applicable_fees=[], not_applicable=[], human_review_items=[])

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

    ctx = _fetch_chunk_context(chunk_store, candidates) if chunk_store else ""
    if not ctx:
        log.info("[RetrieverAgent] No chunk context — proceeding with structured data only")

    response = _evaluate_applicability(vessel, candidates, ctx)
    applicable, not_applicable, review_items = _assemble_applicable_fees(candidates, response.verdicts)

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

def _query_candidate_fees(tariff_store, vessel: VesselInput) -> List[dict]:
    """SQL retrieval: port-specific, national (All), and other-port fees.

    'Other' rows are only fetched when the vessel's port has no dedicated tariff
    section in the DB — avoids inflating candidates for well-covered ports like Durban.
    """
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

    port_rows = _fetch(vessel.port)
    national_rows = _fetch(_ALL_PORTS_LABEL)
    other_rows = _fetch(_OTHER_PORTS_LABEL)

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
        new_unmodeled = len(_parse_json_field(d.get("unmodeled_clauses"), []))
        existing_conf = existing.get("extraction_confidence") or 0.0
        new_conf = d.get("extraction_confidence") or 0.0
        if (new_unmodeled, -new_conf) < (existing_unmodeled, -existing_conf):
            best[key] = d
    return list(best.values())


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

    for attempt in range(max_retries):
        try:
            return structured_llm.invoke(messages)
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
        key     = (fee["section"], fee["tariff_fee_item"])
        verdict = verdict_map.get(key)

        if verdict is None or not verdict.applies:
            not_applicable.append(fee)
            continue

        lower, upper = _parse_gt_bounds(fee["vessel_gt_range"])

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
