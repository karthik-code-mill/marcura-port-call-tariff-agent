#!/usr/bin/env python3
"""
Claude Code helper for interactive tariff extraction.
Used exclusively by .claude/commands/extract-tariff.md

This script handles all I/O so Claude can focus on extraction:
  - PDF text extraction via pdfplumber (same as document_preparation_agent.py)
  - Batch state management (resumable, same ingestion_log logic)
  - DB writes (same TariffStore upsert logic)

Subcommands
-----------
read-batches   --pdf FILENAME [--page-start N] [--page-end N] [--two-column]
               [--version-tag TAG | --db PATH]
               → JSON: {doc_id, tariff_year, db_path, total_pages,
                         total_batches, done_count, pending_count,
                         batches: [{batch_start, batch_end, first_page,
                                    last_page, is_done}]}

get-batch      --pdf FILENAME --batch-start N --batch-end M
               [--page-start N] [--page-end N] [--two-column]
               → JSON: {batch_start, batch_end, first_page, last_page,
                         page_contexts: [str, ...]}
               Each page_contexts entry is the formatted text to feed Claude,
               with overlap context from the prior page prepended automatically.

write-batch    --db PATH --doc-id ID --tariff-year YEAR
               --batch-start N --batch-end M
               --payload-file PATH [--version-tag TAG]
               Reads PortTariffPayload JSON from --payload-file, upserts all
               fee items, marks the batch done in ingestion_log.
               → JSON: {items_written, status, port}

db-status      --db PATH --doc-id ID
               → ingestion_log status JSON (same as TariffStore.get_ingestion_status)
"""

import argparse
import json
import logging
import sys
from pathlib import Path

# ── Resolve project root and import from src/ ──────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent.parent  # .claude/scripts/ → .claude/ → project root
sys.path.insert(0, str(BASE_DIR / "src"))

from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env")

from document_preparation_agent import (
    BATCH_SIZE,
    RAW_DIR,
    DB_PATH,
    TariffStore,
    PortTariffPayload,
    doc_id_from_path,
    db_path_for_version,
    load_pdf_pages,
    save_active_version,
    tariff_year_from_name,
)

OVERLAP_LINES = 30  # mirror _OVERLAP_LINES in document_preparation_agent.py

TWO_COL_NOTE = (
    " [TWO-COLUMN LAYOUT: this PDF page contains two side-by-side document "
    "pages. Read both columns left-to-right as continuous fee content.]"
)

TEMP_DIR = BASE_DIR / ".claude" / "temp"

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────────

def resolve_db(args) -> Path:
    """Return DB path: --version-tag → versioned file, --db → explicit path, else default."""
    if hasattr(args, "version_tag") and args.version_tag:
        return db_path_for_version(args.version_tag)
    if hasattr(args, "db") and args.db:
        return Path(args.db)
    return DB_PATH


def build_page_contexts(pages, batch_start: int, batch_end: int, two_column: bool) -> list:
    """
    Build formatted page context strings for one batch.

    Mirrors run_document_preparation() in document_preparation_agent.py:
      - Adds === PAGE N === headers (+ two-column note when requested)
      - Prepends last OVERLAP_LINES lines of the prior page as a
        [PRIOR PAGE CONTEXT] block so cross-page fee sections are preserved.
    """
    # Overlap from the page immediately before this batch
    prev_tail = ""
    if batch_start > 0:
        prev_text = pages[batch_start - 1][1]
        lines = prev_text.splitlines()
        prev_tail = "\n".join(lines[-OVERLAP_LINES:])

    page_contexts = []
    for i in range(batch_start, batch_end):
        page_num, text = pages[i]
        header = f"=== PAGE {page_num} ===" + (TWO_COL_NOTE if two_column else "")

        if i == batch_start and prev_tail:
            ctx = (
                "[PRIOR PAGE CONTEXT — for continuity only; "
                "do not re-extract fees already on that page]\n"
                f"{prev_tail}\n"
                "[END PRIOR PAGE CONTEXT]\n\n"
                f"{header}\n{text}"
            )
        else:
            ctx = f"{header}\n{text}"
        page_contexts.append(ctx)

    return page_contexts


# ── Subcommand: read-batches ───────────────────────────────────────────────

def cmd_read_batches(args):
    """
    Output batch metadata (no page text) so Claude can plan the run.
    Only I/O; no LLM involvement.
    """
    pdf_path = RAW_DIR / args.pdf
    if not pdf_path.exists():
        available = [f.name for f in sorted(RAW_DIR.glob("*.pdf"))]
        print(json.dumps({"error": f"PDF not found: {pdf_path}", "available": available}))
        sys.exit(1)

    page_start = args.page_start or 1
    page_end   = args.page_end   or None
    doc_id     = doc_id_from_path(pdf_path, page_start, page_end)
    tariff_year = tariff_year_from_name(pdf_path.name)
    db_path    = resolve_db(args)

    store = TariffStore(db_path)
    pages = load_pdf_pages(pdf_path, page_start=page_start, page_end=page_end)
    total = len(pages)

    batches = []
    for batch_start in range(0, total, BATCH_SIZE):
        batch_end  = min(batch_start + BATCH_SIZE, total)
        is_done    = store.is_batch_done(doc_id, batch_start, batch_end)
        first_page = pages[batch_start][0]
        last_page  = pages[batch_end - 1][0]
        batches.append({
            "batch_start": batch_start,
            "batch_end":   batch_end,
            "first_page":  first_page,
            "last_page":   last_page,
            "is_done":     is_done,
        })

    store.close()

    done_count    = sum(1 for b in batches if b["is_done"])
    pending_count = sum(1 for b in batches if not b["is_done"])

    print(json.dumps({
        "doc_id":        doc_id,
        "tariff_year":   tariff_year,
        "db_path":       str(db_path),
        "total_pages":   total,
        "total_batches": len(batches),
        "done_count":    done_count,
        "pending_count": pending_count,
        "batches":       batches,
    }, ensure_ascii=False))


# ── Subcommand: get-batch ─────────────────────────────────────────────────

def cmd_get_batch(args):
    """
    Return the page text for ONE batch, ready for Claude to extract from.
    Includes overlap context from the prior page.
    """
    pdf_path = RAW_DIR / args.pdf
    if not pdf_path.exists():
        print(json.dumps({"error": f"PDF not found: {pdf_path}"}))
        sys.exit(1)

    page_start = args.page_start or 1
    page_end   = args.page_end   or None
    pages      = load_pdf_pages(pdf_path, page_start=page_start, page_end=page_end)

    batch_start = args.batch_start
    batch_end   = min(args.batch_end, len(pages))

    if batch_start >= len(pages):
        print(json.dumps({"error": f"batch_start {batch_start} >= total pages {len(pages)}"}))
        sys.exit(1)

    page_contexts = build_page_contexts(pages, batch_start, batch_end, args.two_column)

    first_page = pages[batch_start][0]
    last_page  = pages[batch_end - 1][0]

    print(json.dumps({
        "batch_start":   batch_start,
        "batch_end":     batch_end,
        "first_page":    first_page,
        "last_page":     last_page,
        "page_contexts": page_contexts,
    }, ensure_ascii=False))


# ── Subcommand: write-batch ───────────────────────────────────────────────

def cmd_write_batch(args):
    """
    Read Claude's extracted PortTariffPayload JSON from a file and
    persist it to the DB using the same TariffStore.upsert_fee_item
    logic as document_preparation_agent.py.
    """
    TEMP_DIR.mkdir(parents=True, exist_ok=True)

    payload_file = Path(args.payload_file)
    if not payload_file.exists():
        print(json.dumps({"error": f"Payload file not found: {payload_file}"}))
        sys.exit(1)

    payload_json = payload_file.read_text(encoding="utf-8")
    try:
        payload = PortTariffPayload.model_validate_json(payload_json)
    except Exception as exc:
        print(json.dumps({"error": f"Invalid PortTariffPayload JSON: {exc}",
                          "hint": "Ensure Claude output matches the schema exactly."}))
        sys.exit(1)

    db_path = Path(args.db)
    store   = TariffStore(db_path)

    port        = payload.port.strip()    or "Unknown"
    country     = payload.country.strip() or ""
    currency    = payload.currency.strip() or ""
    tariff_year = args.tariff_year or ""

    written = 0
    errors  = []
    for item in payload.fees:
        try:
            store.upsert_fee_item(
                args.doc_id, port, country, currency, tariff_year, item
            )
            written += 1
        except Exception as exc:
            errors.append(str(exc))
            log.error(f"upsert failed [{item.section} {item.tariff_fee_item}]: {exc}")

    store.log_batch(args.doc_id, args.batch_start, args.batch_end, "done")

    if args.version_tag:
        save_active_version(args.version_tag)

    store.close()
    print(json.dumps({
        "items_written": written,
        "status":        "done",
        "port":          port,
        "errors":        errors,
    }))


# ── Subcommand: db-status ─────────────────────────────────────────────────

def cmd_db_status(args):
    db_path = Path(args.db)
    if not db_path.exists():
        print(json.dumps({"error": f"DB not found: {db_path}"}))
        sys.exit(1)
    store  = TariffStore(db_path)
    status = store.get_ingestion_status(args.doc_id)
    store.close()
    print(json.dumps(status, ensure_ascii=False))


# ── CLI entry point ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Claude Code helper for tariff extraction pipeline"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # ── read-batches ─────────────────────────────────────────────────────
    p = sub.add_parser("read-batches", help="List batch metadata (no page text)")
    p.add_argument("--pdf",          required=True, help="PDF filename in raw directory")
    p.add_argument("--page-start",   type=int, default=1)
    p.add_argument("--page-end",     type=int, default=None)
    p.add_argument("--two-column",   action="store_true", default=False)
    p.add_argument("--db",           default=None, help="Explicit DB path")
    p.add_argument("--version-tag",  default=None, help="e.g. FY2025-26")

    # ── get-batch ─────────────────────────────────────────────────────────
    p = sub.add_parser("get-batch", help="Get page text for one batch (with overlap context)")
    p.add_argument("--pdf",          required=True)
    p.add_argument("--batch-start",  type=int, required=True)
    p.add_argument("--batch-end",    type=int, required=True)
    p.add_argument("--page-start",   type=int, default=1)
    p.add_argument("--page-end",     type=int, default=None)
    p.add_argument("--two-column",   action="store_true", default=False)

    # ── write-batch ───────────────────────────────────────────────────────
    p = sub.add_parser("write-batch", help="Write extracted JSON to DB")
    p.add_argument("--db",           required=True)
    p.add_argument("--doc-id",       required=True)
    p.add_argument("--tariff-year",  default="")
    p.add_argument("--batch-start",  type=int, required=True)
    p.add_argument("--batch-end",    type=int, required=True)
    p.add_argument("--version-tag",  default=None)
    p.add_argument("--payload-file", required=True,
                   help="Path to JSON file containing PortTariffPayload")

    # ── db-status ─────────────────────────────────────────────────────────
    p = sub.add_parser("db-status", help="Show ingestion status for a doc_id")
    p.add_argument("--db",      required=True)
    p.add_argument("--doc-id",  required=True)

    args = parser.parse_args()
    dispatch = {
        "read-batches": cmd_read_batches,
        "get-batch":    cmd_get_batch,
        "write-batch":  cmd_write_batch,
        "db-status":    cmd_db_status,
    }
    dispatch[args.cmd](args)


if __name__ == "__main__":
    main()
