"""
Validation Agent — Stage 1, Step 3: Second-pass Rule Validation
================================================================
After rule_extractor_agent writes fee items to the DB, this agent performs
a second LLM pass to verify that the extracted records are faithful to the
source Markdown.

Validation targets: items with extraction_confidence < 0.6 OR non-empty
unmodeled_clauses — the records most likely to contain inaccuracies.

On-hold disposition:
  - Record stays in DB (not deleted)
  - extraction_confidence updated to 0.0 (flags it for RetrievalGuardrail)
  - Entry written to config/validation_holds.json

Public interface:
    from agents.validation_agent import run
    summary = run(md_path, store, doc_id, country, version_tag)
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, SystemMessage
from opentelemetry import trace
from pydantic import BaseModel

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from agents.rule_extractor_agent import (
    parse_markdown_tree, _has_fee_content,
    _MODEL_CASCADE, _exhausted_models, _is_daily_quota, _parse_retry_delay,
)
from models.document import ValidationSummary, ValidationVerdict
from monitoring.telemetry import get_tracer

log    = logging.getLogger(__name__)
tracer = get_tracer("tariff.validation_agent")

_BASE_DIR    = Path(__file__).resolve().parent.parent.parent
_PROMPTS_DIR = _BASE_DIR / "prompts"
_HOLDS_PATH  = _BASE_DIR / "config" / "validation_holds.json"

# Model cascade shared with rule_extractor_agent (_MODEL_CASCADE, _exhausted_models).
# If extraction already exhausted a model, validation skips it automatically.
_SYSTEM_PROMPT    = (_PROMPTS_DIR / "validation_agent_sp_v3.0.md").read_text(encoding="utf-8")
_INTER_CALL_DELAY = 3


# ─── LLM validation call ─────────────────────────────────────────────────────

class _RawVerdict(BaseModel):
    verdict: str
    reason:  str


def _call_validation_llm(source_md: str, record: dict, max_retries: int = 3) -> _RawVerdict:
    messages = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=(
            f"SOURCE SECTION:\n{source_md}\n\n"
            f"EXTRACTED RECORD:\n{json.dumps(record, indent=2)}"
        )),
    ]

    available = [m for m in _MODEL_CASCADE if m not in _exhausted_models]
    if not available:
        log.warning("[ValidationAgent] All models quota-exhausted — skipping validation (marking valid)")
        return _RawVerdict(verdict="valid", reason="validation skipped: all models daily-quota-exhausted")

    for model in available:
        structured_llm = init_chat_model(model=model, temperature=0.0).with_structured_output(_RawVerdict)

        for attempt in range(max_retries):
            try:
                result = structured_llm.invoke(messages)
                if result.verdict not in ("valid", "on_hold"):
                    result = _RawVerdict(verdict="valid", reason=result.reason)
                return result

            except Exception as exc:
                if "RESOURCE_EXHAUSTED" in str(exc) and _is_daily_quota(exc):
                    _exhausted_models.add(model)
                    remaining = [m for m in _MODEL_CASCADE if m not in _exhausted_models]
                    log.warning(
                        f"[ValidationAgent] Daily quota exhausted for {model} — "
                        f"cascading to: {remaining[0] if remaining else 'none'}"
                    )
                    break  # try next model

                retry_delay = _parse_retry_delay(exc)
                if attempt < max_retries - 1:
                    wait = max(retry_delay, 2 ** attempt)
                    log.warning(
                        f"[ValidationAgent] LLM call failed "
                        f"(attempt {attempt + 1}/{max_retries}, model={model}): {exc} "
                        f"— retry in {wait}s"
                    )
                    time.sleep(wait)
                else:
                    log.warning(
                        f"[ValidationAgent] All {max_retries} attempts failed on {model}: {exc}"
                    )
                    break  # try next model

    log.warning("[ValidationAgent] All models/retries exhausted — marking as valid (conservative)")
    return _RawVerdict(verdict="valid", reason="validation skipped: all retries exhausted")


# ─── Source section lookup ────────────────────────────────────────────────────

def _build_section_map(full_md: str) -> dict:
    """Map section numbers to their full Markdown content."""
    sections = parse_markdown_tree(full_md)
    sec_map  = {}

    def _collect(secs):
        for sec in secs:
            if sec.number:
                sec_map[sec.number] = sec.full_content
            _collect(sec.children)

    _collect(sections)
    return sec_map


# ─── Holds file persistence ───────────────────────────────────────────────────

def _load_holds() -> dict:
    if _HOLDS_PATH.exists():
        try:
            return json.loads(_HOLDS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_holds(holds: dict) -> None:
    _HOLDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _HOLDS_PATH.write_text(json.dumps(holds, indent=2, ensure_ascii=False), encoding="utf-8")


# ─── Public interface ─────────────────────────────────────────────────────────

def run(
    md_path: Path,
    store,
    doc_id: str,
    country: str = "",
    version_tag: str = "",
) -> ValidationSummary:
    """
    Validate extracted fee items against source Markdown.

    Items with extraction_confidence < 0.6 OR non-empty unmodeled_clauses
    are checked.  On-hold items have their confidence set to 0.0 in the DB
    and are recorded in validation_holds.json.

    Per-item exceptions are caught — on failure the item is conservatively
    marked 'valid' so the pipeline is not blocked by transient LLM errors.
    """
    with tracer.start_as_current_span("doc_prep.validate") as span:
        span.set_attribute("doc_id",  doc_id)
        span.set_attribute("country", country)

        # Load source Markdown and build section lookup
        try:
            full_md   = md_path.read_text(encoding="utf-8")
            sec_map   = _build_section_map(full_md)
        except OSError as exc:
            log.error(f"[ValidationAgent] Cannot read MD file {md_path}: {exc}")
            return ValidationSummary(doc_id=doc_id, total_checked=0, passed=0, on_hold_count=0)

        # Query DB for candidate items
        try:
            rows = store.conn.execute(
                """
                SELECT section, tariff_fee_item, extraction_confidence, unmodeled_clauses
                FROM tariff_fee_items
                WHERE doc_id = ?
                  AND (extraction_confidence < 0.6 OR (unmodeled_clauses IS NOT NULL AND unmodeled_clauses != '[]'))
                ORDER BY section
                """,
                (doc_id,),
            ).fetchall()
        except Exception as exc:
            log.error(f"[ValidationAgent] DB query failed: {exc}")
            return ValidationSummary(doc_id=doc_id, total_checked=0, passed=0, on_hold_count=0)

        log.info(f"[ValidationAgent] {len(rows)} items queued for validation  doc_id={doc_id}")
        span.set_attribute("items_to_validate", len(rows))

        verdicts:     List[ValidationVerdict] = []
        on_hold_items: List[ValidationVerdict] = []
        holds = _load_holds()

        for row in rows:
            section  = row["section"]
            fee_item = row["tariff_fee_item"]
            record   = {"section": section, "tariff_fee_item": fee_item,
                        "extraction_confidence": row["extraction_confidence"],
                        "unmodeled_clauses": row["unmodeled_clauses"]}

            source_md = sec_map.get(section, "")
            if not source_md:
                log.debug(f"[ValidationAgent] No source MD for section '{section}' — skipping")
                continue

            try:
                raw_verdict = _call_validation_llm(source_md, record)
                verdict = ValidationVerdict(
                    section=section,
                    tariff_fee_item=fee_item,
                    verdict=raw_verdict.verdict,
                    reason=raw_verdict.reason,
                )
                verdicts.append(verdict)

                if verdict.verdict == "on_hold":
                    on_hold_items.append(verdict)
                    log.warning(
                        f"[ValidationAgent] ON_HOLD  section={section}  "
                        f"item={fee_item}  reason={verdict.reason[:80]}"
                    )
                    # Flag in DB: set confidence to 0.0 to trigger RetrievalGuardrail
                    try:
                        store.conn.execute(
                            "UPDATE tariff_fee_items SET extraction_confidence = 0.0 "
                            "WHERE doc_id = ? AND section = ? AND tariff_fee_item = ?",
                            (doc_id, section, fee_item),
                        )
                        store.conn.commit()
                    except Exception as db_exc:
                        log.error(f"[ValidationAgent] Failed to update DB for on_hold item: {db_exc}")

                    # Record in holds JSON
                    key = f"{section}__{fee_item}"
                    holds.setdefault(doc_id, {})[key] = {
                        "reason":    verdict.reason,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "country":   country,
                        "version":   version_tag,
                    }
                else:
                    log.debug(f"[ValidationAgent] valid  section={section}  item={fee_item}")

            except Exception as exc:
                log.error(f"[ValidationAgent] Unexpected error for section='{section}' item='{fee_item}': {exc}", exc_info=True)
                # Conservative: treat as valid so pipeline continues
                verdicts.append(ValidationVerdict(section=section, tariff_fee_item=fee_item, verdict="valid", reason=f"validation error: {exc}"))

            time.sleep(_INTER_CALL_DELAY)

        # Persist holds
        if on_hold_items:
            try:
                _save_holds(holds)
                log.info(f"[ValidationAgent] {len(on_hold_items)} items written to {_HOLDS_PATH.name}")
            except OSError as exc:
                log.error(f"[ValidationAgent] Could not save holds file: {exc}")

        passed_count   = sum(1 for v in verdicts if v.verdict == "valid")
        on_hold_count  = len(on_hold_items)

        span.set_attribute("passed",   passed_count)
        span.set_attribute("on_hold",  on_hold_count)

        summary = ValidationSummary(
            doc_id=doc_id,
            total_checked=len(verdicts),
            passed=passed_count,
            on_hold_count=on_hold_count,
            on_hold_items=on_hold_items,
        )
        log.info(
            f"[ValidationAgent] complete  "
            f"checked={len(verdicts)}  passed={passed_count}  on_hold={on_hold_count}"
        )
        return summary
