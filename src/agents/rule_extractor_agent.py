"""
Rule Extractor Agent — Stage 1, Step 2: Markdown → Structured Fee Rules in DB
==============================================================================
Parses the Markdown produced by parser_agent into a section tree, selects
sections containing fee data (Rule A/B/C), calls Gemini once per section to
extract structured TariffFeeItem records, and persists them to SQLite.

Guardrails applied:
  - LLMOutputGuardrail: validates each Gemini response against PortTariffPayload schema.
  - Prompt injection check: section Markdown is sanitized before LLM prompt assembly.

Public interface:
    from agents.rule_extractor_agent import run, ExtractionError
    summary = run(md_path, store, doc_id, tariff_year, country, version_tag)
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, SystemMessage
from opentelemetry import trace
from tqdm import tqdm

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))  # project root

from models.document import ExtractionSummary, MarkdownSection
from models.tariff_extraction import PortTariffPayload
from monitoring.telemetry import get_tracer
from guardrails.llm_output_guardrail import GuardrailViolationError, LLMOutputGuardrail
from security.prompt_injection import InjectionDetectedError, sanitize

log    = logging.getLogger(__name__)
tracer = get_tracer("tariff.rule_extractor_agent")

_BASE_DIR    = Path(__file__).resolve().parent.parent.parent
_PROMPTS_DIR = _BASE_DIR / "prompts"

# Provider + model set via LLM_MODEL env var — same pattern as retriever/calculator.
# Examples: "google_genai:gemini-2.5-flash"  "anthropic:claude-sonnet-4-6"  "openai:gpt-4o"
_llm = init_chat_model(
    model=os.getenv("LLM_MODEL", "google_genai:gemini-2.5-flash"),
    temperature=0.0,
)

_INTER_BATCH_DELAY = 5
_MIN_FEE_CHARS     = 60
_SECTION_NUM_RE    = re.compile(r"^(\d+(?:\.\d+){0,3})\s+(.*)")
_SYSTEM_PROMPT     = (_PROMPTS_DIR / "rule_extractor_agent_sp_v3.0.md").read_text(encoding="utf-8")
_llm_guardrail     = LLMOutputGuardrail()


class ExtractionError(Exception):
    """Raised when the LLM call fails unrecoverably after all retries."""


# ─── LLM call ─────────────────────────────────────────────────────────────────

def _extract_section_fees(section_md: str, section_number: str, section_title: str, max_retries: int = 3) -> PortTariffPayload:
    # Security: sanitize PDF-sourced content before injecting into LLM prompt
    clean_md = sanitize(section_md, source="pdf")

    messages = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=(
            f"Extract all tariff fee items from the section below.\n\n"
            f"## Section Reference\nSection: {section_number or 'N/A'}  Heading: {section_title}\n\n"
            f"## Section Markdown\n\n{clean_md}"
        )),
    ]
    structured_llm = _llm.with_structured_output(PortTariffPayload)

    for attempt in range(max_retries):
        with tracer.start_as_current_span("doc_prep.llm_call") as span:
            span.set_attribute("model",          os.getenv("LLM_MODEL", "google_genai:gemini-2.5-flash"))
            span.set_attribute("section_number", section_number)
            span.set_attribute("attempt",        attempt)
            try:
                result = structured_llm.invoke(messages)
                result = _llm_guardrail.validate(result, PortTariffPayload)
                span.set_attribute("fee_items_count", len(result.fees))
                return result
            except GuardrailViolationError as exc:
                span.set_attribute("guardrail_violation", str(exc))
                log.error(f"[RuleExtractor] Guardrail violation in section '{section_number}': {exc}")
                if attempt == max_retries - 1:
                    raise ExtractionError(f"Schema guardrail failed for section '{section_number}': {exc}") from exc
            except Exception as exc:
                span.set_attribute("error", str(exc))
                if attempt == max_retries - 1:
                    raise ExtractionError(f"LLM call failed for section '{section_number}': {exc}") from exc
                wait = 2 ** attempt
                log.warning(f"[RuleExtractor] LLM failed (attempt {attempt+1}): {exc} — retry in {wait}s")
                time.sleep(wait)

    raise ExtractionError("All LLM models exhausted their daily quota")


# ─── Markdown tree parser ─────────────────────────────────────────────────────

def parse_markdown_tree(markdown: str) -> List[MarkdownSection]:
    roots: List[MarkdownSection] = []
    stack: List[MarkdownSection] = []

    for line in markdown.split("\n"):
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            level = len(m.group(1))
            nm    = _SECTION_NUM_RE.match(m.group(2).strip())
            sec   = MarkdownSection(
                level=level,
                number=nm.group(1) if nm else "",
                title=(nm.group(2).strip() if nm else m.group(2).strip()),
                heading_line=line,
                direct_lines=[],
            )
            while stack and stack[-1].level >= level:
                stack.pop()
            (stack[-1].children if stack else roots).append(sec)
            stack.append(sec)
        elif stack:
            stack[-1].direct_lines.append(line)

    return roots


def _has_fee_content(content: str) -> bool:
    s = content.strip()
    if not s:
        return False
    lines = s.split("\n")
    if sum(1 for l in lines if l.strip().startswith("|")) >= 2:
        return True
    non_heading = " ".join(l for l in lines if not re.match(r"^#{1,6}\s+", l) and l.strip())
    return len(non_heading) >= _MIN_FEE_CHARS


def _find_sections_to_process(sections: List[MarkdownSection]) -> List[MarkdownSection]:
    units: List[MarkdownSection] = []
    for sec in sections:
        if _has_fee_content(sec.direct_content):
            units.append(sec)
        elif sec.children:
            units.extend(_find_sections_to_process(sec.children))
    return units


def _annotate_section_tree(sections: List[MarkdownSection], parent_selected: bool = False) -> List[dict]:
    result = []
    for sec in sections:
        if parent_selected:
            rule, selected = "included_in_parent", False
            children = _annotate_section_tree(sec.children, parent_selected=True)
        elif _has_fee_content(sec.direct_content):
            rule, selected = "A", True
            children = _annotate_section_tree(sec.children, parent_selected=True)
        elif sec.children:
            rule, selected = "B", False
            children = _annotate_section_tree(sec.children, parent_selected=False)
        else:
            rule, selected, children = "C", False, []
        result.append({
            "number": sec.number or "", "title": sec.title, "level": sec.level,
            "rule": rule, "selected": selected,
            "direct_content_chars": len(sec.direct_content), "children": children,
        })
    return result


def _log_annotated_tree(nodes: List[dict], indent: int = 0) -> None:
    prefix = "  " * indent
    for n in nodes:
        tag   = "[SEND]" if n["selected"] else f"[{n['rule']}]"
        num   = f"{n['number']} " if n["number"] else ""
        chars = f"  ({n['direct_content_chars']} chars)" if n["direct_content_chars"] else ""
        log.info(f"{prefix}{tag} {num}{n['title']}{chars}")
        if n["children"]:
            _log_annotated_tree(n["children"], indent + 1)


# ─── Public interface ─────────────────────────────────────────────────────────

def run(
    md_path: Path,
    store,
    doc_id: str,
    tariff_year: str,
    country: str = "",
    version_tag: Optional[str] = None,
) -> ExtractionSummary:
    """
    Parse *md_path* → section tree → LLM extraction per section → SQLite.

    Returns ExtractionSummary for the orchestrator state.
    Raises ExtractionError only if all LLM models are quota-exhausted.
    Per-section errors are caught, logged, and recorded in ingestion_log.
    """
    import hashlib

    with tracer.start_as_current_span("doc_prep.extract") as span:
        span.set_attribute("doc_id",      doc_id)
        span.set_attribute("country",     country)
        span.set_attribute("version_tag", version_tag or "")
        span.set_attribute("md_file",     md_path.name)

        # TODO(future-enhancement): Support reading from object storage (S3/Azure Blob)
        #   by abstracting md_path into a content-provider interface.
        try:
            full_md = md_path.read_text(encoding="utf-8")
        except OSError as exc:
            log.error(f"[RuleExtractor] Cannot read Markdown file {md_path}: {exc}")
            raise ExtractionError(f"Cannot read Markdown file {md_path.name}: {exc}") from exc

        all_secs = parse_markdown_tree(full_md)
        annotated = _annotate_section_tree(all_secs)
        units     = _find_sections_to_process(all_secs)

        # Write section graph JSON alongside the MD file
        graph_path = md_path.with_name(md_path.stem + "-graph.json")
        graph_payload = {
            "md_file": md_path.name, "doc_id": doc_id,
            "total_top_level": len(all_secs), "processing_units": len(units),
            "rules": {"A": "direct fee content → LLM call", "B": "recurse to children", "C": "skip"},
            "tree": annotated,
        }
        try:
            graph_path.write_text(json.dumps(graph_payload, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            log.warning(f"[RuleExtractor] Could not write graph JSON: {exc}")

        log.info("=" * 60)
        log.info(f"[RuleExtractor] {md_path.name} — {len(units)} sections selected")
        _log_annotated_tree(annotated)
        log.info("=" * 60)

        if version_tag is None:
            version_tag = tariff_year or hashlib.md5(f"{md_path.name}:{doc_id}".encode()).hexdigest()[:8]

        done_count = errors_count = skipped_count = fee_items_written = 0

        for sec in tqdm(units, desc=md_path.stem, unit="section"):
            section_id = sec.section_id

            if store.is_section_done(doc_id, section_id):
                skipped_count += 1
                continue

            section_md = sec.full_content
            if len(section_md.strip()) < _MIN_FEE_CHARS:
                log.debug(f"[RuleExtractor] Skipping thin section '{section_id}'")
                continue

            try:
                payload  = _extract_section_fees(section_md, sec.number, sec.title)
                c        = country or payload.country.strip()
                currency = payload.currency.strip()

                for item in payload.fees:
                    if not item.section_name:
                        item.section_name = sec.title
                    store.upsert_fee_item(doc_id, c, currency, tariff_year, item)
                    fee_items_written += 1

                store.log_section(doc_id, section_id, "done")
                done_count += 1
                log.info(f"  [RuleExtractor] {sec.number or '—'} {sec.title[:40]}: {len(payload.fees)} item(s)")

            except InjectionDetectedError as exc:
                errors_count += 1
                store.log_section(doc_id, section_id, f"error:injection:{exc}")
                log.error(f"[RuleExtractor] Injection blocked in section '{section_id}': {exc}")

            except ExtractionError:
                # Quota exhaustion — surface immediately to orchestrator
                span.set_attribute("quota_exhausted", True)
                raise

            except Exception as exc:
                errors_count += 1
                store.log_section(doc_id, section_id, f"error:{exc}")
                log.error(f"[RuleExtractor] Section '{section_id}' failed: {exc}", exc_info=True)

            time.sleep(_INTER_BATCH_DELAY)

        span.set_attribute("sections_done",        done_count)
        span.set_attribute("sections_errors",      errors_count)
        span.set_attribute("fee_items_written",    fee_items_written)

        summary = ExtractionSummary(
            doc_id=doc_id,
            total_sections=len(units),
            done=done_count,
            errors=errors_count,
            fee_items_written=fee_items_written,
            skipped=skipped_count,
        )
        log.info(
            f"[RuleExtractor] complete  version={version_tag}  "
            f"{fee_items_written} fee items  "
            f"{done_count} done  {errors_count} errors  {skipped_count} skipped"
        )
        return summary
