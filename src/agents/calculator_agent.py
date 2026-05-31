"""
Calculator Agent — Stage 2 §2.3.2
====================================
Autonomous agent. Single public interface: run().

Internal steps (fully encapsulated — not exposed to orchestrator):
  §2.3.2.1  Deterministic formula evaluation — Python eval, no LLM involved
  §2.3.2.2  LLM audit pass — validates amounts, flags exceptions
            Invoice assembly
"""

import json
import logging
import math
import os
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
from models.retrieval import ApplicableFeeRecord
from models.invoice import (
    AuditResponse,
    AuditVerdict,
    ComputedLineItem,
    TariffInvoice,
    TariffLineItem,
)

log = logging.getLogger(__name__)

# Configured via LLM_MODEL env var — e.g. "openai:gpt-4o", "anthropic:claude-sonnet-4-6"
_llm = init_chat_model(
    model=os.getenv("LLM_MODEL", "google_genai:gemini-2.5-flash"),
    temperature=0.0,
)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"
_AUDIT_SYSTEM = (_PROMPTS_DIR / "calculator_agent_sp_v3.0.md").read_text(encoding="utf-8")

# Sandboxed globals for formula eval — no builtins, only math helpers
_EVAL_GLOBALS: Dict[str, Any] = {
    "__builtins__": None,
    "math":    math,
    "ceil":    math.ceil,
    "ceiling": math.ceil,
    "floor":   math.floor,
    "round":   round,
    "max":     max,
    "min":     min,
    "abs":     abs,
    "int":     int,
    "float":   float,
}


# ─── Public Interface ─────────────────────────────────────────────────────────

def run(
    vessel: VesselInput,
    applicable_fees: List[ApplicableFeeRecord],
    not_applicable: List[dict],
    human_review_items: List[str],
) -> TariffInvoice:
    """
    Compute a TariffInvoice from applicable fees.
    Deterministic formula evaluation, then LLM audit, then invoice assembly.
    """
    log.info(f"[CalculatorAgent] {len(applicable_fees)} fees  port={vessel.port}  GT={vessel.gross_tonnage}")

    computed: List[ComputedLineItem] = []
    for fee in applicable_fees:
        line = _compute_line(fee, vessel)
        computed.append(line)
        log.info(
            f"  {fee.section}  {fee.tariff_fee_item[:40]:<40}  "
            f"base={line.base_amount:>12,.2f}  "
            f"surcharge={line.surcharge_amount:>10,.2f}  "
            f"total={line.total_amount:>12,.2f}"
            + ("  [FORMULA ERROR]" if line.computation_error else "")
        )

    fee_map = {(f.section, f.tariff_fee_item): f for f in applicable_fees}
    audit   = _audit_lines(computed, fee_map, vessel) if computed else None

    invoice = _assemble_invoice(
        vessel=vessel,
        computed_lines=computed,
        applicable_fees=applicable_fees,
        audit=audit,
        not_applicable=not_applicable,
        human_review_items=human_review_items,
    )
    log.info(f"[CalculatorAgent] Invoice: {len(invoice.line_items)} items  subtotal={invoice.subtotal:,.2f}")
    return invoice


# ─── Internal Steps ───────────────────────────────────────────────────────────

def _eval_formula(
    formula: str,
    gt: float,
    base_fee: float,
    increment: float,
    lower_bound: float,
    upper_bound: float,
) -> float:
    if not formula:
        return base_fee
    locals_ns: Dict[str, Any] = {
        "GT": gt, "gt": gt,
        "base_fee": base_fee, "increment": increment,
        "lower_bound": lower_bound, "upper_bound": upper_bound,
    }
    return float(eval(formula, _EVAL_GLOBALS, locals_ns))  # noqa: S307


def _compute_line(fee: ApplicableFeeRecord, vessel: VesselInput) -> ComputedLineItem:
    gt = vessel.gross_tonnage
    formula_inputs = {
        "GT": gt, "base_fee": fee.base_fee,
        "increment": fee.incremental_fee_per_100_gt,
        "lower_bound": fee.gt_lower_bound, "upper_bound": fee.gt_upper_bound,
    }

    computation_error = ""
    try:
        base_amount = _eval_formula(
            fee.formula, gt, fee.base_fee, fee.incremental_fee_per_100_gt,
            fee.gt_lower_bound, fee.gt_upper_bound,
        )
    except Exception as exc:
        base_amount = fee.base_fee
        computation_error = str(exc)
        log.warning(f"  Formula eval failed [{fee.section} {fee.tariff_fee_item}]: {exc}")

    surcharge_total = 0.0
    applied: List[str] = []
    for s in fee.surcharges:
        condition = s.get("condition", "")
        if condition in fee.active_surcharge_conditions:
            pct = float(s.get("percentage", 0))
            if pct:
                surcharge_total += base_amount * (pct / 100.0)
                applied.append(f"{condition} (+{pct:.0f}%)")

    return ComputedLineItem(
        section=fee.section,
        tariff_fee_item=fee.tariff_fee_item,
        port=fee.port,
        vessel_gt_range=fee.vessel_gt_range,
        base_amount=round(base_amount, 2),
        surcharge_amount=round(surcharge_total, 2),
        total_amount=round(base_amount + surcharge_total, 2),
        formula_used=fee.formula or f"base_fee={fee.base_fee}",
        formula_inputs=formula_inputs,
        active_surcharges=applied,
        computation_error=computation_error,
        source_page=fee.source_page,
    )


def _audit_lines(
    computed: List[ComputedLineItem],
    fee_map: Dict[Tuple[str, str], ApplicableFeeRecord],
    vessel: VesselInput,
    max_retries: int = 3,
) -> AuditResponse:
    review_payload = []
    for line in computed:
        fee = fee_map.get((line.section, line.tariff_fee_item))
        review_payload.append({
            "section":              line.section,
            "tariff_fee_item":      line.tariff_fee_item,
            "formula_used":         line.formula_used,
            "formula_inputs":       line.formula_inputs,
            "base_amount":          line.base_amount,
            "surcharge_amount":     line.surcharge_amount,
            "total_amount":         line.total_amount,
            "active_surcharges":    line.active_surcharges,
            "computation_error":    line.computation_error,
            "exceptions":           fee.exceptions        if fee else [],
            "unmodeled_clauses":    fee.unmodeled_clauses  if fee else [],
            "extraction_confidence": fee.extraction_confidence if fee else 1.0,
        })

    vessel_summary = (
        f"Port={vessel.port}, GT={vessel.gross_tonnage}, "
        f"type={vessel.vessel_type}, voyage={vessel.voyage_type}, "
        f"after_hours={vessel.after_hours}, public_holiday={vessel.public_holiday}, "
        f"in_ballast={vessel.in_ballast}"
    )

    messages = [
        SystemMessage(content=_AUDIT_SYSTEM),
        HumanMessage(content=(
            f"VESSEL: {vessel_summary}\n\n"
            f"COMPUTED LINE ITEMS ({len(review_payload)}):\n"
            f"{json.dumps(review_payload, indent=2)}\n\n"
        )),
    ]
    structured_llm = _llm.with_structured_output(AuditResponse)

    for attempt in range(max_retries):
        try:
            return structured_llm.invoke(messages)
        except Exception as exc:
            if attempt == max_retries - 1:
                raise
            wait = 2 ** attempt
            log.warning(f"[CalculatorAgent] Audit LLM failed (attempt {attempt + 1}) — retrying in {wait}s: {exc}")
            time.sleep(wait)


def _assemble_invoice(
    vessel: VesselInput,
    computed_lines: List[ComputedLineItem],
    applicable_fees: List[ApplicableFeeRecord],
    audit: Optional[AuditResponse],
    not_applicable: List[dict],
    human_review_items: List[str],
) -> TariffInvoice:
    if not computed_lines:
        return TariffInvoice(
            port=vessel.port,
            vessel_type=vessel.vessel_type,
            gross_tonnage=vessel.gross_tonnage,
            voyage_type=vessel.voyage_type,
            line_items=[],
            subtotal=0.0,
            computation_notes="No applicable fees found.",
            human_review_items=list(human_review_items),
            retriever_skipped_fees=len(not_applicable),
        )

    fee_map: Dict[Tuple[str, str], ApplicableFeeRecord] = {
        (f.section, f.tariff_fee_item): f for f in applicable_fees
    }
    audit_map: Dict[Tuple[str, str], AuditVerdict] = (
        {(v.section, v.tariff_fee_item): v for v in audit.verdicts} if audit else {}
    )

    line_items: List[TariffLineItem] = []
    human_review: List[str] = list(human_review_items)

    for line in computed_lines:
        fee     = fee_map.get((line.section, line.tariff_fee_item))
        verdict = audit_map.get((line.section, line.tariff_fee_item))

        needs_review = bool(
            line.computation_error
            or (fee     and fee.needs_human_review)
            or (verdict and verdict.flag_for_human_review)
        )
        if needs_review and line.tariff_fee_item not in human_review:
            human_review.append(line.tariff_fee_item)

        review_reason = " | ".join(filter(None, [
            line.computation_error,
            fee.review_reason     if fee     else "",
            verdict.review_reason if verdict else "",
        ]))

        notes_parts: List[str] = []
        if fee and fee.notes:
            notes_parts.append("; ".join(v for v in fee.notes.values() if v))
        if verdict and verdict.audit_notes:
            notes_parts.append(verdict.audit_notes)

        line_items.append(TariffLineItem(
            section=line.section,
            tariff_item=line.tariff_fee_item,
            port=line.port,
            gt_range=line.vessel_gt_range,
            base_amount=line.base_amount,
            surcharge_amount=line.surcharge_amount,
            total=line.total_amount,
            formula_used=line.formula_used,
            active_surcharges=line.active_surcharges,
            notes="; ".join(notes_parts),
            source_page=line.source_page,
            needs_human_review=needs_review,
            review_reason=review_reason,
        ))

    subtotal  = round(sum(li.total for li in line_items), 2)
    comp_note = (audit.overall_notes if audit else "") or f"{len(line_items)} fee items computed deterministically."

    return TariffInvoice(
        port=vessel.port,
        vessel_type=vessel.vessel_type,
        gross_tonnage=vessel.gross_tonnage,
        voyage_type=vessel.voyage_type,
        line_items=line_items,
        subtotal=subtotal,
        computation_notes=comp_note,
        human_review_items=human_review,
        retriever_skipped_fees=len(not_applicable),
    )
