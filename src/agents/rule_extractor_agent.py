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
# ─── Model cascade config ─────────────────────────────────────────────────────
# Primary model from LLM_MODEL env var; LLM_FALLBACK_MODELS is a comma-separated
# list of additional models to try when the primary hits its daily quota.
# Each model has its own per-model-per-day quota on the free tier, so switching
# models is the only recovery path for daily exhaustion.
_PRIMARY_MODEL = os.getenv("LLM_MODEL", "google_genai:gemini-2.5-flash")
_MODEL_CASCADE: List[str] = list(dict.fromkeys(
    m.strip() for m in
    os.getenv(
        "LLM_FALLBACK_MODELS",
        f"{_PRIMARY_MODEL},google_genai:gemini-2.0-flash,google_genai:gemini-1.5-flash",
    ).split(",")
    if m.strip()
))
_exhausted_models: set = set()   # models whose daily quota is gone for this process run

_INTER_BATCH_DELAY   = 5
_MIN_FEE_CHARS       = 60
_SECTION_NUM_RE      = re.compile(r"^(\d+(?:\.\d+){0,3})\s+(.*)")
# Leading digit must be 1-9: real section numbers (1.1, 3.6, 4.1.1...).
# Excludes table footnotes like "0.50 Represents workboat" where 0 is a cell value.
_NUMBERED_HEADING_RE = re.compile(r"^[1-9]\d*(?:\.\d+)*\s+\S")
_BR_RE               = re.compile(r"<br\s*/?>", re.IGNORECASE)
_SYSTEM_PROMPT       = (_PROMPTS_DIR / "rule_extractor_agent_sp_v3.0.md").read_text(encoding="utf-8")
_llm_guardrail       = LLMOutputGuardrail()


# ─── Retry helpers ────────────────────────────────────────────────────────────

def _is_daily_quota(exc: Exception) -> bool:
    """True when the 429 is a per-model daily limit, not a transient RPM limit."""
    return "PerDay" in str(exc)


def _parse_retry_delay(exc: Exception) -> int:
    """Return the API-suggested retryDelay in seconds (0 if absent)."""
    m = re.search(r"retryDelay[^0-9]*(\d+)s", str(exc), re.IGNORECASE)
    return int(m.group(1)) if m else 0


class ExtractionError(Exception):
    """Raised when the LLM call fails unrecoverably after all retries."""


# ─── LLM call ─────────────────────────────────────────────────────────────────

def _extract_section_fees(
    section_md: str,
    section_number: str,
    section_title: str,
    max_retries: int = 3,
) -> PortTariffPayload:
    # TABLE_JSON injection already done as a parser step in run() before this call.
    clean_md = sanitize(section_md, source="pdf")
    messages = [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=(
            f"Extract all tariff fee items from the section below.\n\n"
            f"## Section Reference\nSection: {section_number or 'N/A'}  Heading: {section_title}\n\n"
            f"## Section Markdown\n\n{clean_md}"
        )),
    ]

    available = [m for m in _MODEL_CASCADE if m not in _exhausted_models]
    if not available:
        raise ExtractionError(f"All models daily-quota-exhausted (section '{section_number}')")

    for model in available:
        structured_llm = init_chat_model(model=model, temperature=0.0).with_structured_output(PortTariffPayload)

        for attempt in range(max_retries):
            with tracer.start_as_current_span("doc_prep.llm_call") as span:
                span.set_attribute("model",          model)
                span.set_attribute("section_number", section_number)
                span.set_attribute("attempt",        attempt)
                try:
                    result = structured_llm.invoke(messages)
                    result = _llm_guardrail.validate(result, PortTariffPayload)
                    span.set_attribute("fee_items_count", len(result.fees))
                    if model != _PRIMARY_MODEL:
                        tqdm.write(f"  [CASCADE] success via {model} for section '{section_number}'")
                    return result

                except GuardrailViolationError as exc:
                    span.set_attribute("guardrail_violation", str(exc))
                    log.error(f"[RuleExtractor] Guardrail violation in '{section_number}': {exc}")
                    if attempt == max_retries - 1:
                        raise ExtractionError(f"Schema guardrail failed for '{section_number}': {exc}") from exc

                except Exception as exc:
                    span.set_attribute("error", str(exc))

                    if "RESOURCE_EXHAUSTED" in str(exc) and _is_daily_quota(exc):
                        # Daily per-model quota — blacklist and try next model immediately
                        _exhausted_models.add(model)
                        remaining = [m for m in _MODEL_CASCADE if m not in _exhausted_models]
                        msg = (
                            f"[RuleExtractor] Daily quota exhausted for {model} — "
                            f"cascading to: {remaining[0] if remaining else 'none'}"
                        )
                        log.warning(msg)
                        tqdm.write(f"  [QUOTA] {msg}")
                        span.set_attribute("quota_exhausted_model", model)
                        break  # exit attempt loop → try next model

                    # Transient / RPM rate-limit — honour the API-suggested retryDelay
                    retry_delay = _parse_retry_delay(exc)
                    if attempt < max_retries - 1:
                        wait = max(retry_delay, 2 ** attempt)
                        msg = (
                            f"[RuleExtractor] LLM failed "
                            f"(attempt {attempt + 1}/{max_retries}, model={model}): {exc} "
                            f"— retry in {wait}s"
                        )
                        log.warning(msg)
                        tqdm.write(f"  [RETRY] wait {wait}s ('{section_number}')")
                        time.sleep(wait)
                    else:
                        log.error(
                            f"[RuleExtractor] All {max_retries} attempts failed on {model} "
                            f"for '{section_number}': {exc}"
                        )
                        break  # try next model in cascade

    raise ExtractionError(f"All models in cascade failed for section '{section_number}'")


# ─── Markdown pre-processing ─────────────────────────────────────────────────

def _normalize_content_headings(markdown: str) -> str:
    """
    Convert unnumbered ATX headings to bold inline text before tree building.

    Two-column PDF layout produces a flat heading structure: pymupdf4llm assigns
    the same ATX level (##) to everything on each column half-page. Bold text
    that acts as a content sub-heading inside a section — "Payable by:",
    "Exemptions", "All other vessels" — ends up as a ## sibling of the numbered
    tariff section rather than as content inside it.

    Result without this fix (flat siblings):
        ## 1.1.1 LIGHT DUES      ← empty direct content → Rule C (skipped)
        ## Payable by:           ← 1520 chars sent to LLM without parent context
        ## Exemptions            ← 448 chars sent separately

    Result with this fix (content inline):
        ## 1.1.1 LIGHT DUES      ← direct content includes all fee data → Rule A
        **Payable by:**          ← kept as bold text, stays in 1.1.1's content
        **Exemptions**           ← same

    Only headings that do NOT start with a section number are demoted.
    Numbered headings (1.1, 3.6, 4.1.1 …) are left unchanged.
    """
    result = []
    for line in markdown.split("\n"):
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            heading_text = m.group(2).strip()
            # pymupdf4llm wraps bold PDF headings in **...**  e.g. "## **1.1 LIGHT DUES**"
            # Strip surrounding bold markers before the number-check so we don't demote
            # real numbered section headings that happen to be bold-formatted.
            bare = re.sub(r"^\*+|\*+$", "", heading_text).strip()
            if not _NUMBERED_HEADING_RE.match(bare):
                result.append(f"**{heading_text}**")
            else:
                result.append(line)
        else:
            result.append(line)
    return "\n".join(result)


def _expand_merged_table_rows(markdown: str) -> str:
    """
    pymupdf4llm merges adjacent PDF table rows that sit close together by
    joining cell text with <br> inside a single markdown table row.

    This function splits them back when ALL non-empty value columns (col 2+)
    contain the same non-zero number of <br> separators — which reliably
    signals N values packed into one markdown cell.

    Label column (col 1) is split on <br> too: the first part goes to row 0,
    all remaining parts are joined (space-separated) into the last row.

    Only data rows (after the |---| separator) are expanded; header rows
    are left unchanged so multi-line column headers stay intact.

    Example — section 3.9 RUNNING OF VESSEL LINES:
      MERGED:   |Per service<br>If outside ordinary hours...|2406<br>4812|...|
      EXPANDED: |Per service|2406|...|
                |If outside ordinary hours...|4812|...|
    """
    lines = markdown.split("\n")
    result = []
    past_separator = False

    for line in lines:
        stripped = line.strip()

        if not stripped.startswith("|"):
            past_separator = False
            result.append(line)
            continue

        # Separator row: |---|---|---|
        if re.match(r"^\|(?:[\s:]*-+[\s:]*\|)+$", stripped):
            past_separator = True
            result.append(line)
            continue

        # Header row (before first separator) — leave unchanged
        if not past_separator:
            result.append(line)
            continue

        # Data row — parse cells
        cells = stripped.split("|")
        if cells and cells[0] == "":
            cells = cells[1:]
        if cells and cells[-1] == "":
            cells = cells[:-1]

        if len(cells) < 2:
            result.append(line)
            continue

        label_cell  = cells[0]
        value_cells = cells[1:]
        non_empty   = [c for c in value_cells if c.strip()]

        if not non_empty:
            result.append(line)
            continue

        br_counts = [len(_BR_RE.findall(c)) for c in non_empty]

        # Expand only when all value cells agree on a non-zero <br> count
        if len(set(br_counts)) != 1 or br_counts[0] == 0:
            result.append(line)
            continue

        n_rows       = br_counts[0] + 1
        label_parts  = _BR_RE.split(label_cell)
        value_splits = [_BR_RE.split(c) for c in value_cells]

        for i in range(n_rows):
            if i < n_rows - 1:
                lbl = label_parts[i].strip() if i < len(label_parts) else ""
            else:
                lbl = " ".join(p.strip() for p in label_parts[i:]).strip()

            row_vals = [
                vsplit[i].strip() if i < len(vsplit) else ""
                for vsplit in value_splits
            ]
            result.append("|" + "|".join([lbl] + row_vals) + "|")

    return "\n".join(result)


def _clean_cell(raw: str) -> str:
    """Normalise a single table cell for the structured JSON representation."""
    # Normalize problematic Unicode characters from PDF rendering
    text = raw.replace('�', '-')   # replacement character → hyphen
    text = text.replace('–', '-')  # en dash
    text = text.replace('—', '-')  # em dash
    text = text.replace('‒', '-')  # figure dash
    text = text.replace('‑', '-')  # non-breaking hyphen
    # Join all <br> fragments with a space
    text = _BR_RE.sub(" ", text)
    # Strip markdown bold/italic markers
    text = re.sub(r"\*+", "", text)
    # Fix PDF hyphenated line-break splits: "Sal- danha" → "Saldanha"
    text = re.sub(r"(\w)-\s+(\w)", r"\1\2", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    # Clean PDF number-spacing artifact universally: "32 864.53" → "32864.53",
    # "10 001" → "10001" — applied to all cells including labels with embedded numbers
    text = re.sub(r"(\d)\s+(\d)", r"\1\2", text)
    return text


def _to_float(s: str) -> Optional[float]:
    """Convert a cleaned cell string to float; returns None for n/a or empty."""
    s = s.strip()
    if not s or s.lower() in ("n/a", "na", "-", ""):
        return None
    try:
        return float(re.sub(r"[,\s]", "", s))
    except ValueError:
        return None


def _is_increment_row(label: str) -> bool:
    """True if this expanded row carries per-100-ton increment data (not a base fee).

    Increment rows always START with 'Plus' (e.g. 'Plus Per 100 tons or part thereof
    above 2 000').  A label like '100 000 plus' or 'Above 100 000' is a GT band label,
    not an increment row, so we require the leading position.
    """
    l = label.lower().strip()
    return l.startswith("plus") or "per 100 ton" in l or "per 100 gt" in l or "increment" in l


def _parse_gt_bounds(label: str) -> Optional[tuple]:
    """
    Extract (gt_min, gt_max) from a row label. Returns None if no GT range found.
    Handles: 'Up to 2 000', '2 001 to 10 000', '50 001 to 100 000', 'Above 100 000'.
    """
    s = re.sub(r"[,\s]", "", label.lower())
    m = re.search(r"upto(\d+)", s)
    if m:
        return (0, int(m.group(1)))
    m = re.search(r"above(\d+)", s)
    if m:
        return (int(m.group(1)) + 1, 999_999_999)
    m = re.search(r"(\d+)to(\d+)", s)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return None


def _build_fee_records(raw_headers: List[str], parsed_rows: List[dict]) -> List[dict]:
    """
    Convert the matrix-style (headers × rows) table into a flat list of fee records
    that the LLM can map directly to TariffFeeItem fields.

    Three patterns detected automatically:

    1. GT-banded  — rows alternate base / "Plus per 100 tons" pairs.
       Output: {port, gt_range, base_fee, incremental_fee_per_100_tons}

    2. Simple base + increment  — exactly 2 rows: first is base, second is per-100-ton.
       Output: {port, base_fee, incremental_fee_per_100_tons}

    3. Multi-fee rows  — multiple rows with distinct fee descriptions (no Plus rows).
       Output: {port, fee_label, base_fee}
    """
    # Build port_col_map: display_port_name → raw header key in row dict.
    # Handles PDF joint-port headers like "Port Elizabeth / Ngqura" by emitting
    # two display port names that both map back to the combined header key.
    port_col_map: dict = {}
    for h in raw_headers[1:]:
        if not h:
            continue
        if "/" in h:
            for part in h.split("/"):
                p = part.strip()
                if p:
                    port_col_map[p] = h
        else:
            port_col_map[h] = h

    port_cols = list(port_col_map.keys())
    if not port_cols or not parsed_rows:
        return []

    has_plus_rows = any(_is_increment_row(r["label"]) for r in parsed_rows)

    # ── Pattern 1: GT-banded ──────────────────────────────────────────────────
    if has_plus_rows:
        records: List[dict] = []
        i = 0
        while i < len(parsed_rows):
            row = parsed_rows[i]
            if _is_increment_row(row["label"]):
                i += 1
                continue  # plus row consumed by the preceding base row

            gt_bounds = _parse_gt_bounds(row["label"])
            gt_range  = f"{gt_bounds[0]}-{gt_bounds[1]}" if gt_bounds else ""

            # Look ahead for the immediately following increment row
            plus_row: Optional[dict] = None
            if i + 1 < len(parsed_rows) and _is_increment_row(parsed_rows[i + 1]["label"]):
                plus_row = parsed_rows[i + 1]

            for port in port_cols:
                hdr_key  = port_col_map[port]
                base_val = _to_float(row.get(hdr_key, ""))
                if base_val is None:
                    continue  # n/a port × band combo
                incr_val = _to_float(plus_row.get(hdr_key, "")) if plus_row else None
                rec: dict = {"port": port}
                if gt_range:
                    rec["gt_range"] = gt_range
                rec["base_fee"] = base_val
                rec["incremental_fee_per_100_tons"] = incr_val if incr_val is not None else 0.0
                records.append(rec)  # one record per port per GT band

            i += 1
            if plus_row:
                i += 1  # skip the consumed plus row

        return records

    # ── Pattern 2: simple base + per-100-ton (exactly 2 rows) ────────────────
    if (len(parsed_rows) == 2
            and ("per 100" in parsed_rows[1]["label"].lower()
                 or "increment" in parsed_rows[1]["label"].lower())):
        base_row = parsed_rows[0]
        incr_row = parsed_rows[1]
        records = []
        for port in port_cols:
            hdr_key  = port_col_map[port]
            base_val = _to_float(base_row.get(hdr_key, ""))
            incr_val = _to_float(incr_row.get(hdr_key, ""))
            if base_val is None:
                continue
            records.append({
                "port": port,
                "base_fee": base_val,
                "incremental_fee_per_100_tons": incr_val if incr_val is not None else 0.0,
            })
        return records

    # ── Pattern 3: multi-fee rows (each row = distinct fee) ──────────────────
    records = []
    for row in parsed_rows:
        for port in port_cols:
            hdr_key = port_col_map[port]
            val = _to_float(row.get(hdr_key, ""))
            if val is None:
                continue
            records.append({
                "port": port,
                "fee_label": row["label"],
                "base_fee": val,
            })
    return records


def _inject_parsed_tables(section_md: str) -> str:
    """
    For every markdown table in *section_md*, parse it into a pre-solved list of
    fee records and inject that as a [TABLE_JSON] block immediately after the table.

    The records are structured so the LLM maps fields directly to TariffFeeItem
    without having to interpret column headers or pair base/increment rows itself.

    Three output formats depending on table structure:
      - {port, base_fee, incremental_fee_per_100_tons}          simple base+increment
      - {port, gt_range, base_fee, incremental_fee_per_100_tons} GT-banded
      - {port, fee_label, base_fee}                              multi-fee rows
    """
    lines = section_md.split("\n")
    result: List[str] = []
    i = 0

    while i < len(lines):
        line = lines[i]
        if not line.strip().startswith("|"):
            result.append(line)
            i += 1
            continue

        table_block: List[str] = []
        while i < len(lines) and lines[i].strip().startswith("|"):
            table_block.append(lines[i])
            i += 1

        # Do NOT add the raw table yet — only emit it if JSON generation fails.

        header_rows: List[str] = []
        data_rows:   List[str] = []
        past_sep = False
        for tl in table_block:
            s = tl.strip()
            if re.match(r"^\|(?:[\s:]*-+[\s:]*\|)+$", s):
                past_sep = True
                continue
            (data_rows if past_sep else header_rows).append(s)

        if not header_rows or not data_rows:
            # Malformed table — keep raw so the LLM sees something
            result.extend(table_block)
            continue

        def _split_cells(row_str: str) -> List[str]:
            parts = row_str.split("|")
            if parts and parts[0].strip() == "":
                parts = parts[1:]
            if parts and parts[-1].strip() == "":
                parts = parts[:-1]
            return parts

        raw_headers = [_clean_cell(c) for c in _split_cells(header_rows[-1])]

        parsed_rows: List[dict] = []
        for dl in data_rows:
            raw_cells = [_clean_cell(c) for c in _split_cells(dl)]
            if not any(raw_cells):
                continue
            label = raw_cells[0] if raw_cells else ""
            row: dict = {"label": label}
            for col_idx, hdr in enumerate(raw_headers[1:], start=1):
                val = raw_cells[col_idx] if col_idx < len(raw_cells) else ""
                if hdr and val:
                    row[hdr] = val
            parsed_rows.append(row)

        if parsed_rows:
            fee_records = _build_fee_records(raw_headers, parsed_rows)
            if fee_records:
                # Replace noisy pipe table entirely with clean pre-solved records
                block = json.dumps(fee_records, indent=2, ensure_ascii=False)
                result += ["[TABLE_JSON]", block, "[/TABLE_JSON]"]
            else:
                # Could not produce fee records — keep raw table + raw matrix JSON
                # so the LLM still has something to work with
                result.extend(table_block)
                raw_payload = {"headers": raw_headers, "rows": parsed_rows}
                block = json.dumps(raw_payload, indent=2, ensure_ascii=False)
                result += ["", "[TABLE_JSON]", block, "[/TABLE_JSON]"]
        else:
            # Empty parsed rows — keep raw table
            result.extend(table_block)

    return "\n".join(result)


# ─── Markdown tree parser ─────────────────────────────────────────────────────

def parse_markdown_tree(markdown: str) -> List[MarkdownSection]:
    roots: List[MarkdownSection] = []
    stack: List[MarkdownSection] = []

    for line in markdown.split("\n"):
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            level = len(m.group(1))
            # Strip surrounding bold markers before number/title extraction —
            # pymupdf4llm produces "## **1.1 LIGHT DUES**" from bold PDF headings.
            bare  = re.sub(r"^\*+|\*+$", "", m.group(2).strip()).strip()
            nm    = _SECTION_NUM_RE.match(bare)
            sec   = MarkdownSection(
                level=level,
                number=nm.group(1) if nm else "",
                title=(nm.group(2).strip() if nm else bare),
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


def _has_numbered_children(sec: "MarkdownSection") -> bool:
    """True if at least one direct child carries a section number (e.g. '3.3.1').

    Children without numbers are content headings that pymupdf4llm promotes to
    Markdown ATX headings from bold text in the PDF ('Payable by:', 'Exemptions',
    'All other vessels').  These are NOT independent processing units — they belong
    to the parent section's content and should be sent with it.
    """
    return any(c.number for c in sec.children)


def _find_sections_to_process(sections: List[MarkdownSection]) -> List[MarkdownSection]:
    units: List[MarkdownSection] = []
    for sec in sections:
        if _has_fee_content(sec.direct_content):
            # Rule A — direct fee content: send this section.
            units.append(sec)
        elif sec.children:
            if _has_numbered_children(sec):
                # Rule B — real sub-sections present: recurse into them.
                units.extend(_find_sections_to_process(sec.children))
            elif _has_fee_content(sec.full_content):
                # Rule D — all children are unnumbered content headings
                # (e.g. "Payable by:", "Exemptions", "All other vessels").
                # pymupdf4llm renders PDF bold text as ATX headings, splitting
                # what is logically one section into fake sub-sections.
                # Send the full section so the LLM sees all fee data in one chunk.
                units.append(sec)
            # else: no fee content anywhere — skip (Rule C)
        # else: no children, no direct content — Rule C skip
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
            if _has_numbered_children(sec):
                rule, selected = "B", False
                children = _annotate_section_tree(sec.children, parent_selected=False)
            elif _has_fee_content(sec.full_content):
                # Rule D — unnumbered content headings only; send full section.
                rule, selected = "D", True
                children = _annotate_section_tree(sec.children, parent_selected=True)
            else:
                rule, selected, children = "C", False, []
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
        line  = f"{prefix}{tag} {num}{n['title']}{chars}"
        log.info(line)
        print(line)
        if n["children"]:
            _log_annotated_tree(n["children"], indent + 1)


# ─── Public interface ─────────────────────────────────────────────────────────

def prepare_markdown(md_path: Path) -> tuple:
    """
    Apply all parser intelligence steps to *md_path* and write the result to
    *<stem>-enhanced.md* in the same directory.  Returns (content, enhanced_path).

    Steps in order:
      1. _normalize_content_headings  — demote unnumbered ATX headings to bold inline
      2. _expand_merged_table_rows    — split <br>-merged PDF table rows
      3. _inject_parsed_tables        — replace pipe tables with TABLE_JSON blocks

    Call this before run() when you want to inspect the enhanced MD without
    triggering any LLM calls (--parse-only mode).
    """
    try:
        full_md = md_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ExtractionError(f"Cannot read Markdown file {md_path.name}: {exc}") from exc

    full_md = _normalize_content_headings(full_md)
    full_md = _expand_merged_table_rows(full_md)
    full_md = _inject_parsed_tables(full_md)

    enhanced_path = md_path.with_name(md_path.stem + "-enhanced.md")
    try:
        enhanced_path.write_text(full_md, encoding="utf-8")
        msg = f"[RuleExtractor] enhanced MD → {enhanced_path.name}"
        log.info(msg)
        print(msg)
    except OSError as exc:
        log.warning(f"[RuleExtractor] Could not write enhanced MD: {exc}")
        return full_md, enhanced_path

    # Remove the raw file so only the enhanced version remains on disk
    try:
        md_path.unlink()
        log.info(f"[RuleExtractor] removed raw MD: {md_path.name}")
    except OSError as exc:
        log.warning(f"[RuleExtractor] Could not remove raw MD {md_path.name}: {exc}")

    return full_md, enhanced_path


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

        try:
            full_md, enhanced_path = prepare_markdown(md_path)
        except ExtractionError:
            raise
        except OSError as exc:
            log.error(f"[RuleExtractor] Cannot read Markdown file {md_path}: {exc}")
            raise ExtractionError(f"Cannot read Markdown file {md_path.name}: {exc}") from exc

        span.set_attribute("md_file", enhanced_path.name)

        all_secs  = parse_markdown_tree(full_md)
        annotated = _annotate_section_tree(all_secs)
        units     = _find_sections_to_process(all_secs)

        # Write section graph JSON alongside the enhanced MD file
        graph_path = enhanced_path.with_name(enhanced_path.stem + "-graph.json")
        graph_payload = {
            "md_file": enhanced_path.name, "doc_id": doc_id,
            "total_top_level": len(all_secs), "processing_units": len(units),
            "rules": {"A": "direct fee content → LLM call", "B": "recurse to children", "C": "skip"},
            "tree": annotated,
        }
        try:
            graph_path.write_text(json.dumps(graph_payload, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            log.warning(f"[RuleExtractor] Could not write graph JSON: {exc}")

        separator = "=" * 60
        header    = f"[RuleExtractor] {enhanced_path.name} — {len(units)} sections selected"
        log.info(separator)
        log.info(header)
        print(separator)
        print(header)
        _log_annotated_tree(annotated)
        log.info(separator)
        print(separator)

        if version_tag is None:
            version_tag = tariff_year or hashlib.md5(f"{md_path.name}:{doc_id}".encode()).hexdigest()[:8]

        done_count = errors_count = skipped_count = fee_items_written = 0

        bar = tqdm(units, desc=md_path.stem, unit="section")
        for sec in bar:
            section_id = sec.section_id

            if store.is_section_done(doc_id, section_id):
                skipped_count += 1
                tqdm.write(f"  [SKIP] {sec.number or '—'} {sec.title[:40]}")
                continue

            section_md = sec.full_content
            if len(section_md.strip()) < _MIN_FEE_CHARS:
                log.debug(f"[RuleExtractor] Skipping thin section '{section_id}'")
                continue

            bar.set_postfix_str(f"{sec.number} {sec.title[:30]}")
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
                msg = f"  [OK]   {sec.number or '—'} {sec.title[:40]}: {len(payload.fees)} item(s)"
                log.info(msg)
                tqdm.write(msg)

            except InjectionDetectedError as exc:
                errors_count += 1
                store.log_section(doc_id, section_id, f"error:injection:{exc}")
                msg = f"  [BLOCK] Injection in '{section_id}': {exc}"
                log.error(msg)
                tqdm.write(msg)

            except ExtractionError:
                # Quota exhaustion — surface immediately to orchestrator
                span.set_attribute("quota_exhausted", True)
                raise

            except Exception as exc:
                errors_count += 1
                store.log_section(doc_id, section_id, f"error:{exc}")
                msg = f"  [ERR]  '{section_id}': {exc}"
                log.error(msg)
                tqdm.write(msg)

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
