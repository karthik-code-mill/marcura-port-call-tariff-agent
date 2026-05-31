"""
Parser Agent — Stage 1, Step 1: PDF → Markdown
================================================
Single responsibility: convert a PDF file to a Markdown file on disk.
Decoupled from LLM extraction — purely a document conversion step.

Extension point: pass a custom `converter_fn` to swap pymupdf4llm for
any future LLM-based or cloud-based PDF converter without changing
the public interface or the orchestrator.

Public interface:
    from agents.parser_agent import run, ParserError
    md_path = run(pdf_path, page_start=5, country="South Africa", version_tag="FY2025-26-v3.2")
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Callable, Optional

import fitz
import pymupdf4llm
from opentelemetry import trace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.document_preparation_agent import md_path_for, OUT_DIR
from monitoring.telemetry import get_tracer

log    = logging.getLogger(__name__)
tracer = get_tracer("tariff.parser_agent")


class ParserError(Exception):
    """Raised when PDF conversion fails unrecoverably."""


# ─── Default converter (pymupdf4llm) ─────────────────────────────────────────

def _pymupdf_convert(
    pdf_path: Path,
    page_start: int,
    page_end: Optional[int],
    two_column_layout: bool,
) -> str:
    """Convert PDF pages to Markdown using pymupdf4llm + fitz."""
    doc   = fitz.open(str(pdf_path))
    total = len(doc)
    end   = min((page_end or total), total)
    pages = list(range(page_start - 1, end))

    if two_column_layout:
        split_doc = fitz.open()
        for idx in pages:
            src   = doc[idx]
            r     = src.rect
            mid_x = r.x0 + (r.x1 - r.x0) / 2
            for clip in (fitz.Rect(r.x0, r.y0, mid_x, r.y1), fitz.Rect(mid_x, r.y0, r.x1, r.y1)):
                pg = split_doc.new_page(width=clip.width, height=clip.height)
                pg.show_pdf_page(pg.rect, doc, idx, clip=clip)
        md = pymupdf4llm.to_markdown(split_doc, page_chunks=False)
        split_doc.close()
    else:
        md = pymupdf4llm.to_markdown(doc, pages=pages, page_chunks=False)

    doc.close()
    return md


# ─── Public interface ─────────────────────────────────────────────────────────

def run(
    pdf_path: Path,
    page_start: int = 1,
    page_end: Optional[int] = None,
    two_column_layout: bool = False,
    country: str = "",
    version_tag: str = "",
    output_dir: Path = OUT_DIR,
    converter_fn: Callable[..., str] = _pymupdf_convert,
    delete_after_parse: bool = False,
) -> Path:
    """
    Convert *pdf_path* to Markdown and write the result to disk.

    Parameters
    ----------
    converter_fn:
        Callable with signature (pdf_path, page_start, page_end, two_column_layout) -> str.
        Defaults to pymupdf4llm. Swap for an LLM-based converter without changing callers.
    delete_after_parse:
        When True, the source PDF is deleted after the Markdown file is successfully
        written. Deletion failure logs a warning but does not raise — the Markdown is
        already on disk and the pipeline continues normally.

    Returns
    -------
    Path to the written Markdown file.

    Raises
    ------
    ParserError on any conversion failure (wraps the underlying exception).
    """
    page_count = (page_end or 0) - page_start + 1

    with tracer.start_as_current_span("doc_prep.parse") as span:
        span.set_attribute("country",      country)
        span.set_attribute("version_tag",  version_tag)
        span.set_attribute("pdf_name",     pdf_path.name)
        span.set_attribute("page_start",   page_start)
        span.set_attribute("page_end",     page_end or -1)
        span.set_attribute("page_count",   page_count)
        span.set_attribute("converter",    converter_fn.__name__)

        if country and version_tag:
            out_path = md_path_for(country, version_tag)
        else:
            out_path = output_dir / f"{pdf_path.stem}_v2.md"

        log.info(f"[ParserAgent] start  pdf={pdf_path.name}  pages={page_start}–{page_end or 'end'}  out={out_path.name}")

        try:
            md = converter_fn(pdf_path, page_start, page_end, two_column_layout)
        except (fitz.FitzError, IOError, OSError) as exc:
            log.exception(f"[ParserAgent] PDF conversion failed: {exc}")
            span.set_attribute("error", str(exc))
            raise ParserError(f"PDF conversion failed for {pdf_path.name}: {exc}") from exc
        except Exception as exc:
            log.exception(f"[ParserAgent] Unexpected error during conversion: {exc}")
            span.set_attribute("error", str(exc))
            raise ParserError(f"Unexpected error for {pdf_path.name}: {exc}") from exc

        # Write result — separate try block so disk errors are surfaced as ParserError,
        # not masked by the converter's exception handling above.
        # TODO(future-enhancement): Stream large Markdown files to disk in chunks to
        #   avoid OOM on very dense multi-hundred-page PDFs.
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(md, encoding="utf-8")
        except OSError as exc:
            log.exception(f"[ParserAgent] Failed to write Markdown output: {exc}")
            span.set_attribute("error", str(exc))
            raise ParserError(f"Failed to write output {out_path}: {exc}") from exc

        span.set_attribute("md_chars", len(md))
        log.info(f"[ParserAgent] done   {out_path.name}  ({len(md):,} chars)")

        if delete_after_parse:
            try:
                pdf_path.unlink()
                span.set_attribute("source_pdf_deleted", True)
                log.info(f"[ParserAgent] deleted source PDF: {pdf_path.name}")
            except OSError as exc:
                # Markdown is already written — deletion failure is non-fatal.
                span.set_attribute("source_pdf_deleted", False)
                log.warning(f"[ParserAgent] could not delete source PDF {pdf_path.name}: {exc}")

    return out_path
