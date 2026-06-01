"""LangGraph state contract for the Document Preparation pipeline (Stage 1)."""

from __future__ import annotations

import operator
from pathlib import Path
from typing import Annotated, List, Optional

from typing_extensions import TypedDict

from models.document import ExtractionSummary, ValidationSummary


class DocumentPrepState(TypedDict):
    # ── Inputs (set before graph invocation) ─────────────────────────────────
    pdf_path:          Path
    country:           str
    version_tag:       str
    page_start:        int
    page_end:          Optional[int]
    two_column_layout: bool
    skip_parse:        bool   # True → use md_path directly, skip node_parse
    delete_after_parse: bool  # True → remove the source PDF once Markdown is written
    parse_only:        bool   # True → run parser intelligence steps only, skip LLM extraction

    # ── Inter-node outputs ────────────────────────────────────────────────────
    md_path:            Optional[Path]             # written by node_parse
    doc_id:             Optional[str]              # written by node_parse or set by caller
    extraction_summary: Optional[ExtractionSummary]  # written by node_extract
    validation_summary: Optional[ValidationSummary]  # written by node_validate

    # ── Diagnostics (append-only via LangGraph operator.add) ─────────────────
    errors:   Annotated[List[str], operator.add]
    step_log: Annotated[List[str], operator.add]
