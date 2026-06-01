"""
Document Preparation Pipeline Orchestrator — Stage 1
=====================================================
LangGraph state graph for the three-step document preparation pipeline:

  START → [parse] → [extract] → [validate] → END

This is a SEPARATE orchestrator from orchestrator/pipeline.py (Stage 2).
Stage 2 handles vessel tariff calculation (retriever + calculator).
Stage 1 handles tariff document ingestion (PDF → Markdown → DB rules → validation).

Graph topology:
  parse   — parser_agent.run()         : PDF → Markdown file
  extract — rule_extractor_agent.run() : Markdown → SQLite fee records
  validate — validation_agent.run()    : Cross-check DB records vs source MD

Use --skip-parse to run extract + validate on an existing Markdown file.

CLI:
    python -m orchestrator.document_prep_pipeline --country "South Africa" \\
        --version-tag FY2025-26-v3.2 --page-start 5

    python -m orchestrator.document_prep_pipeline --country "South Africa" \\
        --version-tag FY2025-26-v3.2 --skip-parse \\
        --md-path ../context-layer/rag/out/south-africa-tariff-book-FY2025-26-v3.2.md
"""

from __future__ import annotations

import hashlib
import logging
import sys
from pathlib import Path
from typing import Optional

from langgraph.graph import END, START, StateGraph

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents import parser_agent, rule_extractor_agent, validation_agent
from agents.document_preparation_agent import (
    TariffStore,
    _doc_id,
    _tariff_year_from_name,
    db_path_for_version,
    save_active_version,
)
from agents.parser_agent import ParserError
from agents.rule_extractor_agent import ExtractionError, prepare_markdown
from models.document import ExtractionSummary, ValidationSummary
from orchestrator.document_prep_state import DocumentPrepState

log = logging.getLogger(__name__)


# ─── Graph nodes ──────────────────────────────────────────────────────────────

def _node_parse(state: DocumentPrepState) -> dict:
    if state.get("skip_parse"):
        log.info("[DocPrepPipeline] parse  SKIPPED (skip_parse=True)")
        return {"step_log": ["parse: skipped"]}

    pdf_path = state["pdf_path"]
    log.info(f"[DocPrepPipeline] parse  pdf={pdf_path.name}")
    try:
        md_path = parser_agent.run(
            pdf_path=pdf_path,
            page_start=state.get("page_start", 1),
            page_end=state.get("page_end"),
            two_column_layout=state.get("two_column_layout", False),
            country=state["country"],
            version_tag=state["version_tag"],
            delete_after_parse=state.get("delete_after_parse", False),
        )
        doc_id = _doc_id(pdf_path, state.get("page_start", 1), state.get("page_end"))
        log.info(f"[DocPrepPipeline] parse  done → {md_path.name}")
        return {
            "md_path":  md_path,
            "doc_id":   doc_id,
            "step_log": [f"parse: {md_path.name}"],
        }
    except ParserError as exc:
        log.error(f"[DocPrepPipeline] parse FAILED: {exc}")
        return {"errors": [f"parse: {exc}"], "step_log": ["parse: FAILED"]}
    except Exception as exc:
        log.error(f"[DocPrepPipeline] parse unexpected error: {exc}", exc_info=True)
        return {"errors": [f"parse: {exc}"], "step_log": ["parse: FAILED"]}


def _node_extract(state: DocumentPrepState) -> dict:
    md_path = state.get("md_path")
    doc_id  = state.get("doc_id")

    if not md_path or not md_path.exists():
        msg = "extract: skipped — no Markdown file available"
        log.warning(f"[DocPrepPipeline] {msg}")
        return {"errors": [msg], "step_log": [msg]}

    # ── parse-only mode: run parser intelligence steps, skip LLM ─────────────
    if state.get("parse_only"):
        log.info(f"[DocPrepPipeline] parse-only  md={md_path.name}")
        try:
            _, enhanced_path = prepare_markdown(md_path)
            msg = f"parse-only: enhanced MD written → {enhanced_path.name}"
            log.info(f"[DocPrepPipeline] {msg}")
            return {"md_path": enhanced_path, "step_log": [msg]}
        except Exception as exc:
            log.error(f"[DocPrepPipeline] parse-only FAILED: {exc}", exc_info=True)
            return {"errors": [f"parse-only: {exc}"], "step_log": ["parse-only: FAILED"]}

    pdf_path    = state.get("pdf_path") or md_path
    tariff_year = _tariff_year_from_name(pdf_path.name if hasattr(pdf_path, "name") else str(pdf_path))
    db_path     = db_path_for_version(state["country"], state["version_tag"])
    store       = TariffStore(db_path)

    log.info(f"[DocPrepPipeline] extract  md={md_path.name}  db={db_path.name}")
    try:
        summary = rule_extractor_agent.run(
            md_path=md_path,
            store=store,
            doc_id=doc_id or hashlib.md5(str(md_path).encode()).hexdigest()[:12],
            tariff_year=tariff_year,
            country=state["country"],
            version_tag=state["version_tag"],
        )
        save_active_version(state["country"], state["version_tag"])
        log.info(
            f"[DocPrepPipeline] extract  done — "
            f"{summary.fee_items_written} fees  {summary.errors} errors"
        )
        # prepare_markdown deleted the raw file and wrote *-enhanced.md;
        # update md_path so _node_validate reads the enhanced file.
        enhanced_path = md_path.with_name(md_path.stem + "-enhanced.md")
        return {
            "md_path":            enhanced_path if enhanced_path.exists() else md_path,
            "extraction_summary": summary,
            "step_log": [f"extract: {summary.fee_items_written} fee items, {summary.errors} errors"],
        }
    except ExtractionError as exc:
        log.error(f"[DocPrepPipeline] extract FAILED (quota): {exc}")
        return {"errors": [f"extract: {exc}"], "step_log": ["extract: FAILED (quota)"]}
    except Exception as exc:
        log.error(f"[DocPrepPipeline] extract unexpected error: {exc}", exc_info=True)
        return {"errors": [f"extract: {exc}"], "step_log": ["extract: FAILED"]}
    finally:
        store.close()


def _node_validate(state: DocumentPrepState) -> dict:
    if state.get("parse_only"):
        msg = "validate: skipped (parse-only mode)"
        log.info(f"[DocPrepPipeline] {msg}")
        return {"step_log": [msg]}

    md_path = state.get("md_path")
    doc_id  = state.get("doc_id")

    if not md_path or not md_path.exists():
        msg = "validate: skipped — no Markdown file available"
        log.warning(f"[DocPrepPipeline] {msg}")
        return {"step_log": [msg]}

    db_path = db_path_for_version(state["country"], state["version_tag"])
    store   = TariffStore(db_path)

    log.info(f"[DocPrepPipeline] validate  doc_id={doc_id}")
    try:
        summary = validation_agent.run(
            md_path=md_path,
            store=store,
            doc_id=doc_id or hashlib.md5(str(md_path).encode()).hexdigest()[:12],
            country=state["country"],
            version_tag=state["version_tag"],
        )
        log.info(
            f"[DocPrepPipeline] validate  done — "
            f"checked={summary.total_checked}  on_hold={summary.on_hold_count}"
        )
        return {
            "validation_summary": summary,
            "step_log": [f"validate: {summary.total_checked} checked, {summary.on_hold_count} on_hold"],
        }
    except Exception as exc:
        log.error(f"[DocPrepPipeline] validate unexpected error: {exc}", exc_info=True)
        return {"errors": [f"validate: {exc}"], "step_log": ["validate: FAILED"]}
    finally:
        store.close()


# ─── Graph builder ────────────────────────────────────────────────────────────

def build_document_prep_pipeline():
    """Compile the LangGraph for the document preparation pipeline."""
    builder = StateGraph(DocumentPrepState)
    builder.add_node("parse",    _node_parse)
    builder.add_node("extract",  _node_extract)
    builder.add_node("validate", _node_validate)
    builder.add_edge(START,     "parse")
    builder.add_edge("parse",   "extract")
    builder.add_edge("extract", "validate")
    builder.add_edge("validate", END)
    return builder.compile()


def run_document_prep_pipeline(
    pdf_path: Path,
    country: str,
    version_tag: str,
    page_start: int = 1,
    page_end: Optional[int] = None,
    two_column_layout: bool = False,
    skip_parse: bool = False,
    md_path: Optional[Path] = None,
    delete_after_parse: bool = False,
    parse_only: bool = False,
) -> DocumentPrepState:
    """Build, run, and return the final state. Used by API and CLI."""
    pipeline = build_document_prep_pipeline()
    initial: dict = {
        "pdf_path":           pdf_path,
        "country":            country,
        "version_tag":        version_tag,
        "page_start":         page_start,
        "page_end":           page_end,
        "two_column_layout":  two_column_layout,
        "skip_parse":         skip_parse,
        "delete_after_parse": delete_after_parse,
        "parse_only":         parse_only,
        "md_path":            md_path,
        "doc_id":             None,
        "extraction_summary": None,
        "validation_summary": None,
        "errors":             [],
        "step_log":           [],
    }
    # TODO(future-enhancement): Pass a LangGraph SqliteSaver checkpointer to
    #   persist state between parse/extract/validate so a failed validation run
    #   can resume from extract output without re-running the expensive PDF parse.
    try:
        final = pipeline.invoke(initial)
    except Exception as exc:
        log.error(f"[DocPrepPipeline] LangGraph graph execution failed: {exc}", exc_info=True)
        raise RuntimeError(f"Document prep pipeline failed: {exc}") from exc

    if final.get("errors"):
        log.warning(f"[DocPrepPipeline] completed with errors: {final['errors']}")
    log.info(f"[DocPrepPipeline] step log: {' → '.join(final.get('step_log', []))}")
    return final


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    from datetime import datetime
    from dotenv import load_dotenv

    load_dotenv()

    from agents.document_preparation_agent import RAW_DIR, raw_dir_for

    parser = argparse.ArgumentParser(description="Document Preparation Pipeline — Stage 1")
    parser.add_argument("--country",           default="South Africa")
    parser.add_argument("--version-tag",       required=True)
    parser.add_argument("--page-start",        type=int, default=1)
    parser.add_argument("--page-end",          type=int, default=None)
    parser.add_argument("--two-column-layout", action="store_true", default=False)
    parser.add_argument("--skip-parse",          action="store_true", default=False,
                        help="Skip Pass 1 (PDF→MD). Requires --md-path.")
    parser.add_argument("--md-path",             type=Path, default=None,
                        help="Existing Markdown file. Required when --skip-parse is set.")
    parser.add_argument("--delete-after-parse",  action="store_true", default=False,
                        help="Delete the source PDF after Markdown is successfully written.")
    parser.add_argument("--parse-only",           action="store_true", default=False,
                        help="Run parser intelligence steps only (normalize, expand rows, inject TABLE_JSON). "
                             "Writes *-enhanced.md and stops — no LLM calls, no DB writes.")
    args = parser.parse_args()

    # ── File + console logging ────────────────────────────────────────────────
    _LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs"
    _LOG_DIR.mkdir(exist_ok=True)
    _ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    _log_file = _LOG_DIR / f"doc-prep-{args.version_tag}-{_ts}.log"
    _fmt      = "%(asctime)s  %(levelname)-8s  %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=_fmt,
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(str(_log_file), encoding="utf-8"),
        ],
    )
    log.info(f"[DocPrepPipeline] log file → {_log_file}")

    if args.parse_only and not args.skip_parse and not args.md_path:
        # parse-only without an existing MD still needs a PDF → run parse node first
        pass  # handled normally below; parse node runs, then extract short-circuits

    if args.skip_parse:
        if not args.md_path:
            raise SystemExit("--skip-parse requires --md-path")
        pdf_files = [args.md_path]   # placeholder; pdf_path unused in skip_parse mode
        pdf_path  = args.md_path
    else:
        # Country-specific subdirectory takes priority; fall back to the flat root dir
        # for PDFs placed there before per-country organisation was introduced.
        country_raw = raw_dir_for(args.country)
        pdf_files   = sorted(country_raw.glob("*.pdf"))
        if not pdf_files:
            pdf_files = sorted(RAW_DIR.glob("*.pdf"))
        if not pdf_files:
            raise SystemExit(f"No PDF files found in {country_raw} or {RAW_DIR}")
        pdf_path = pdf_files[0]
        log.info(f"Processing: {pdf_path.name}  (from {pdf_path.parent})")

    final_state = run_document_prep_pipeline(
        pdf_path=pdf_path,
        country=args.country,
        version_tag=args.version_tag,
        page_start=args.page_start,
        page_end=args.page_end,
        two_column_layout=args.two_column_layout,
        skip_parse=args.skip_parse,
        md_path=args.md_path,
        delete_after_parse=args.delete_after_parse,
        parse_only=args.parse_only,
    )

    ext = final_state.get("extraction_summary")
    val = final_state.get("validation_summary")
    if ext:
        log.info(f"Extraction: {ext.fee_items_written} fees, {ext.errors} errors")
    if val:
        log.info(f"Validation: {val.total_checked} checked, {val.on_hold_count} on_hold")
    if final_state.get("errors"):
        log.error(f"Pipeline errors: {final_state['errors']}")
