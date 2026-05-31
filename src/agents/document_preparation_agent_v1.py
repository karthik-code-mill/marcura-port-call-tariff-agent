"""
Document Preparation Agent v1 — Stage 1 §2.2.1  [DEMO / REFERENCE]
====================================================================
Single-pass extraction pipeline (v1 approach):
  PDF  →  pdfplumber (layout-aware)  →  Gemini SDK (structured JSON)  →  SQLite

v1 contrast with v2:
  • Single pass only — no separate Markdown intermediate
  • pdfplumber for PDF reading (vs pymupdf4llm in v2)
  • Page-batch granularity with cross-page overlap context
  • No section_name / vessel_type columns in the DB schema
  • Truncation fallback: per-page re-extraction with overlap prefix

Isolation: this file is self-contained — it does NOT import from or write
to any shared config/module used by the main v2 pipeline. DB files are
written to context-layer/rag/db/v1/ to avoid naming or schema conflicts.

CLI:
    python -m agents.document_preparation_agent_v1 --country "South Africa" --version-tag FY2025-26-v1 --page-start 5
    python -m agents.document_preparation_agent_v1 --list-versions --country "South Africa"
"""

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pdfplumber
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from tqdm import tqdm

load_dotenv()

log = logging.getLogger(__name__)

# ─── Paths (fully self-contained) ─────────────────────────────────────────────

BASE_DIR   = Path(__file__).resolve().parent.parent.parent
RAW_DIR    = BASE_DIR / "context-layer" / "rag" / "raw"
DB_V1_DIR  = BASE_DIR / "context-layer" / "rag" / "db" / "v1"   # isolated from v2

DB_V1_DIR.mkdir(parents=True, exist_ok=True)

BATCH_SIZE    = 2
_OVERLAP_LINES = 30

# ─── Naming helpers ───────────────────────────────────────────────────────────

def _country_slug(country: str) -> str:
    return re.sub(r"\s+", "-", country.strip().lower())


def db_path_for_version(country: str, version_tag: str) -> Path:
    """Return DB path: context-layer/rag/db/v1/{country_slug}-tariff-store-{version}.db"""
    safe = re.sub(r"[^\w\-.]", "_", version_tag)
    return DB_V1_DIR / f"{_country_slug(country)}-tariff-store-{safe}.db"


def list_available_versions(country: str = "") -> List[dict]:
    """Scan rag/db/v1/ for DB files matching the naming convention."""
    results = []
    pattern = f"{_country_slug(country)}-tariff-store-*.db" if country else "*-tariff-store-*.db"
    for db_file in sorted(DB_V1_DIR.glob(pattern)):
        stem   = db_file.stem          # e.g. "south-africa-tariff-store-FY2025-26-v1"
        parts  = stem.split("-tariff-store-", 1)
        c_slug = parts[0] if len(parts) == 2 else ""
        v_tag  = parts[1] if len(parts) == 2 else stem
        results.append({
            "country":     c_slug,
            "version_tag": v_tag,
            "db_path":     str(db_file),
            "db_exists":   True,
        })
    return results


# ─── Gemini setup ─────────────────────────────────────────────────────────────

_client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
_MODELS: List[str] = ["gemini-2.5-flash", "gemini-2.0-flash"]
_exhausted_models: set = set()
_INTER_BATCH_DELAY = 5

_PROMPTS_DIR  = BASE_DIR / "prompts"
_SYSTEM_PROMPT = (_PROMPTS_DIR / "document_preparation_agent_sp_v1.8.md").read_text(encoding="utf-8")

_generation_config = types.GenerateContentConfig(
    system_instruction=_SYSTEM_PROMPT,
    response_mime_type="application/json",
    temperature=0.0,
    max_output_tokens=65536,
)


# ─── Pydantic models (inline — not shared with v2) ───────────────────────────

class SurchargeItem(BaseModel):
    condition: str = Field(
        description=(
            "The full trigger condition for this surcharge, copied verbatim from the document. "
            "E.g. 'Outside ordinary working hours (18:00–06:00)', 'Public holidays', "
            "'Vessel cancels after pilot has boarded'."
        )
    )
    percentage: float = Field(
        description=(
            "The percentage added on top of the base fee when this surcharge applies. "
            "E.g. 50.0 means the total becomes base_fee × 1.50. "
            "Use 0.0 if the surcharge is a flat amount rather than a percentage."
        )
    )


class ExceptionItem(BaseModel):
    text: str = Field(description="Verbatim text of the exception clause as it appears in the document.")
    machine_handled: bool = Field(
        default=False,
        description="True only if the exception has a deterministic, rule-based outcome the system can evaluate automatically.",
    )
    source: Dict[str, Any] = Field(
        default_factory=dict,
        description="Optional source reference, e.g. {'section': '3.4', 'page': 12}.",
    )


class TariffFeeItem(BaseModel):
    section: str = Field(description="Section number exactly as printed, e.g. '3.6', '4.1.2'.")
    tariff_fee_item: str = Field(description="Official fee name in UPPERCASE exactly as in the document.")
    port: str = Field(
        description=(
            "Port this fee applies to. Valid values: "
            "(a) A single normalised port name, e.g. 'Durban', 'Richards Bay'. "
            "(b) 'All' — fee applies to all ports or no specific port is named. "
            "(c) 'Other' — ONLY when the document explicitly says 'other ports' or 'remaining ports'. "
            "Emit one record per port when a table has DIFFERENT numeric values per named port."
        )
    )
    vessel_gt_range: str = Field(
        description=(
            "GT band as 'gt_min-gt_max'. Both bounds mandatory. "
            "'>50 000' → '50001-999999999'; 'All ranges' → '0-999999999'; 'up to 5 000' → '0-5000'."
        )
    )
    base_fee: float = Field(default=0.0)
    incremental_fee_per_100_gt: float = Field(default=0.0)
    formula: str = Field(
        default="",
        description=(
            "Python-evaluable expression. Available vars: GT, base_fee, increment, lower_bound, upper_bound. "
            "Use 'EXTERNAL_DETERMINATION_REFERENCED_SKIP' for statutory pass-through fees."
        ),
    )
    conditions: List[str] = Field(default_factory=list)
    surcharges: List[SurchargeItem] = Field(default_factory=list)
    exceptions: List[ExceptionItem] = Field(default_factory=list)
    notes: Dict[str, str] = Field(default_factory=dict)
    unmodeled_clauses: List[str] = Field(default_factory=list)
    extraction_confidence: float = Field(default=1.0)
    source_page: int = Field(default=0)


class PortTariffPayload(BaseModel):
    port:     str                 = Field(description="Port name from document title/header.")
    country:  str                 = Field(default="")
    currency: str                 = Field(default="")
    fees:     List[TariffFeeItem] = Field(default_factory=list)


# ─── LLM call with retry + truncation fallback ────────────────────────────────

def _parse_retry_delay(exc: Exception) -> Optional[int]:
    m = re.search(r"['\"]retryDelay['\"]\s*:\s*['\"](\d+)s['\"]", str(exc))
    return int(m.group(1)) if m else None


def _is_daily_quota(exc: Exception) -> bool:
    return "PerDay" in str(exc)


def _extract_batch(page_contexts: List[str], max_retries: int = 3) -> PortTariffPayload:
    context = "\n\n".join(page_contexts)
    schema  = PortTariffPayload.model_json_schema()
    prompt  = (
        "Extract all tariff fee items from the document pages below. "
        "Return JSON matching this schema exactly:\n\n"
        f"{json.dumps(schema, indent=2)}\n\n"
        "Pages:\n\n"
        f"{context}"
    )

    available = [m for m in _MODELS if m not in _exhausted_models]
    if not available:
        raise RuntimeError("All models in the cascade have exhausted their daily quota")

    for model in available:
        for attempt in range(max_retries):
            try:
                response = _client.models.generate_content(model=model, contents=prompt, config=_generation_config)
                return PortTariffPayload.model_validate_json(response.text)
            except Exception as exc:
                err_str = str(exc)

                # Truncated JSON — fall back to per-page extraction with overlap prefix
                if "EOF while parsing" in err_str and len(page_contexts) > 1:
                    log.warning(f"Truncated JSON on {len(page_contexts)}-page batch — falling back to per-page with overlap")
                    merged    = PortTariffPayload(port="", country="", currency="", fees=[])
                    prev_tail = ""
                    for page_ctx in page_contexts:
                        ctx = (
                            "[PRIOR PAGE CONTEXT — do not re-extract]\n"
                            f"{prev_tail}\n[END PRIOR PAGE CONTEXT]\n\n{page_ctx}"
                            if prev_tail else page_ctx
                        )
                        prev_tail = "\n".join(page_ctx.splitlines()[-_OVERLAP_LINES:])
                        try:
                            result = _extract_batch([ctx], max_retries=max_retries)
                            if not merged.port     and result.port:     merged.port     = result.port
                            if not merged.country  and result.country:  merged.country  = result.country
                            if not merged.currency and result.currency: merged.currency = result.currency
                            merged.fees.extend(result.fees)
                        except Exception as page_exc:
                            log.error(f"  Per-page fallback failed: {page_exc}")
                    return merged

                if "RESOURCE_EXHAUSTED" in err_str and _is_daily_quota(exc):
                    _exhausted_models.add(model)
                    remaining = [m for m in _MODELS if m not in _exhausted_models]
                    log.warning(f"Daily quota exhausted for {model} — cascading to: {remaining[0] if remaining else 'none'}")
                    break

                if attempt == max_retries - 1:
                    raise
                wait = _parse_retry_delay(exc) or 2 ** attempt
                log.warning(f"LLM call failed ({model}, attempt {attempt + 1}): {exc} — retrying in {wait}s")
                time.sleep(wait)

    raise RuntimeError("All models in the cascade have exhausted their daily quota")


# ─── GT range parser ──────────────────────────────────────────────────────────

def parse_gt_range(gt_range: str) -> Tuple[float, float]:
    s = gt_range.strip().lower().replace(",", "").replace(" ", "")
    if not s or s in ("all", "allranges", "allrange", "allvessels"):
        return 0.0, 999_999_999.0
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
            return val, 999_999_999.0
        if any(x in s for x in ("<", "below", "under")):
            return 0.0, val
        return val, val
    return 0.0, 999_999_999.0


# ─── SQLite persistence (v1 schema — no section_name / vessel_type) ───────────

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tariff_fee_items (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id                      TEXT    NOT NULL,
    port                        TEXT    NOT NULL,
    country                     TEXT    DEFAULT '',
    currency                    TEXT    DEFAULT '',
    tariff_year                 TEXT    DEFAULT '',
    section                     TEXT    NOT NULL,
    tariff_fee_item             TEXT    NOT NULL,
    vessel_gt_range             TEXT    DEFAULT 'All ranges',
    gt_min                      REAL    DEFAULT 0,
    gt_max                      REAL    DEFAULT 999999999,
    base_fee                    REAL    DEFAULT 0,
    incremental_fee_per_100_gt  REAL    DEFAULT 0,
    formula                     TEXT    DEFAULT '',
    conditions                  TEXT    DEFAULT '[]',
    surcharges                  TEXT    DEFAULT '[]',
    exceptions                  TEXT    DEFAULT '[]',
    notes                       TEXT    DEFAULT '{}',
    unmodeled_clauses           TEXT    DEFAULT '[]',
    extraction_confidence       REAL    DEFAULT 1.0,
    source_page                 INTEGER DEFAULT 0,
    ingested_at                 TEXT    NOT NULL,
    UNIQUE(doc_id, section, tariff_fee_item, vessel_gt_range)
);

CREATE TABLE IF NOT EXISTS ingestion_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id      TEXT    NOT NULL,
    batch_start INTEGER NOT NULL,
    batch_end   INTEGER NOT NULL,
    status      TEXT    NOT NULL,
    logged_at   TEXT    NOT NULL,
    UNIQUE(doc_id, batch_start, batch_end)
);

CREATE INDEX IF NOT EXISTS idx_fee_port       ON tariff_fee_items(port);
CREATE INDEX IF NOT EXISTS idx_fee_gt         ON tariff_fee_items(gt_min, gt_max);
CREATE INDEX IF NOT EXISTS idx_fee_section    ON tariff_fee_items(section);
CREATE INDEX IF NOT EXISTS idx_fee_confidence ON tariff_fee_items(extraction_confidence);
"""


class TariffStoreV1:
    """v1 persistence layer — separate class name to avoid any import collision."""

    def __init__(self, db_path: Path) -> None:
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA_SQL)
        self.conn.commit()
        log.info(f"TariffStoreV1 ready: {db_path}")

    def upsert_fee_item(self, doc_id: str, port: str, country: str, currency: str, tariff_year: str, item: TariffFeeItem) -> None:
        gt_min, gt_max = parse_gt_range(item.vessel_gt_range)
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            INSERT INTO tariff_fee_items
                (doc_id, port, country, currency, tariff_year, section,
                 tariff_fee_item, vessel_gt_range, gt_min, gt_max,
                 base_fee, incremental_fee_per_100_gt, formula,
                 conditions, surcharges, exceptions, notes,
                 unmodeled_clauses, extraction_confidence, source_page, ingested_at)
            VALUES (?,?,?,?,?,?, ?,?,?,?, ?,?,?, ?,?,?,?, ?,?,?,?)
            ON CONFLICT(doc_id, section, tariff_fee_item, vessel_gt_range)
            DO UPDATE SET
                base_fee                   = excluded.base_fee,
                incremental_fee_per_100_gt = excluded.incremental_fee_per_100_gt,
                gt_min                     = excluded.gt_min,
                gt_max                     = excluded.gt_max,
                formula                    = excluded.formula,
                conditions                 = excluded.conditions,
                surcharges                 = excluded.surcharges,
                exceptions                 = excluded.exceptions,
                notes                      = excluded.notes,
                unmodeled_clauses          = excluded.unmodeled_clauses,
                extraction_confidence      = excluded.extraction_confidence,
                source_page                = excluded.source_page,
                ingested_at                = excluded.ingested_at
            """,
            (
                doc_id, port, country, currency, tariff_year, item.section,
                item.tariff_fee_item, item.vessel_gt_range, gt_min, gt_max,
                item.base_fee, item.incremental_fee_per_100_gt, item.formula,
                json.dumps(item.conditions),
                json.dumps([s.model_dump() for s in item.surcharges]),
                json.dumps([e.model_dump() for e in item.exceptions]),
                json.dumps(item.notes),
                json.dumps(item.unmodeled_clauses),
                item.extraction_confidence, item.source_page, now,
            ),
        )
        self.conn.commit()

    def log_batch(self, doc_id: str, start: int, end: int, status: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            INSERT INTO ingestion_log (doc_id, batch_start, batch_end, status, logged_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(doc_id, batch_start, batch_end)
            DO UPDATE SET status = excluded.status, logged_at = excluded.logged_at
            """,
            (doc_id, start, end, status, now),
        )
        self.conn.commit()

    def is_batch_done(self, doc_id: str, start: int, end: int) -> bool:
        row = self.conn.execute(
            "SELECT status FROM ingestion_log WHERE doc_id=? AND batch_start=? AND batch_end=?",
            (doc_id, start, end),
        ).fetchone()
        return row is not None and row["status"] == "done"

    def get_ingestion_status(self, doc_id: str) -> dict:
        rows = self.conn.execute(
            "SELECT * FROM ingestion_log WHERE doc_id=? ORDER BY batch_start", (doc_id,)
        ).fetchall()
        total_fees = self.conn.execute(
            "SELECT COUNT(*) FROM tariff_fee_items WHERE doc_id=?", (doc_id,)
        ).fetchone()[0]
        batches = [dict(r) for r in rows]
        return {
            "doc_id": doc_id,
            "total_batches": len(batches),
            "done":   sum(1 for b in batches if b["status"] == "done"),
            "errors": sum(1 for b in batches if b["status"].startswith("error")),
            "total_fee_items": total_fees,
            "batches": batches,
        }

    def close(self) -> None:
        self.conn.close()


# ─── PDF loading ──────────────────────────────────────────────────────────────

def load_pdf_pages(pdf_path: Path, page_start: int = 1, page_end: Optional[int] = None) -> List[Tuple[int, str]]:
    pages: List[Tuple[int, str]] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        total     = len(pdf.pages)
        end_idx   = min((page_end or total), total)
        start_idx = max(0, page_start - 1)
        for pdf_page in pdf.pages[start_idx:end_idx]:
            page_num = pdf_page.page_number
            plain    = pdf_page.extract_text(x_tolerance=3, y_tolerance=3) or ""
            tables   = pdf_page.extract_tables()
            if tables:
                table_blocks = [
                    "\n".join("\t".join((cell or "").strip() for cell in row) for row in tbl if any(row))
                    for tbl in tables
                ]
                pages.append((page_num, plain + "\n\n[TABLE]\n" + "\n\n[TABLE]\n".join(table_blocks)))
            else:
                pages.append((page_num, plain))
    return pages


# ─── Helpers ──────────────────────────────────────────────────────────────────

def doc_id_from_path(pdf_path: Path, page_start: int = 1, page_end: Optional[int] = None) -> str:
    key = f"v1:{pdf_path.name}:{page_start}:{page_end or 'end'}"
    return hashlib.md5(key.encode()).hexdigest()[:12]


def _tariff_year_from_name(name: str) -> str:
    m = re.search(r"FY[\-_]?(\d{4}[\-_]\d{2,4}|\d{4})", name, re.IGNORECASE)
    return m.group(1) if m else ""


# ─── Pipeline ─────────────────────────────────────────────────────────────────

def run_document_preparation(
    pdf_path: Path,
    store: TariffStoreV1,
    country: str,
    page_start: int = 1,
    page_end: Optional[int] = None,
    two_column_layout: bool = False,
    version_tag: Optional[str] = None,
) -> str:
    """
    Full v1 pipeline for one PDF:
      1. Load pages with pdfplumber (layout-aware text + table extraction).
      2. Iterate 2-page batches — skip already-completed batches (resumable).
      3. Call Gemini for structured fee extraction per batch.
      4. Persist each TariffFeeItem to SQLite.
      5. Return the version_tag used.
    """
    doc_id      = doc_id_from_path(pdf_path, page_start, page_end)
    tariff_year = _tariff_year_from_name(pdf_path.name)
    version_tag = version_tag or tariff_year or hashlib.md5(f"{pdf_path.name}:{doc_id}".encode()).hexdigest()[:8]

    log.info(f"[v1] Pipeline start: {pdf_path.name}  country={country}  version={version_tag}  doc_id={doc_id}")
    pages = load_pdf_pages(pdf_path, page_start=page_start, page_end=page_end)
    log.info(f"[v1] Loaded {len(pages)} pages")

    _two_col_note = " [TWO-COLUMN LAYOUT: read both columns left-to-right as continuous fee content.]"

    skipped = 0
    for batch_start in tqdm(range(0, len(pages), BATCH_SIZE), desc=pdf_path.stem, unit="batch"):
        batch_end = min(batch_start + BATCH_SIZE, len(pages))

        if store.is_batch_done(doc_id, batch_start, batch_end):
            skipped += 1
            continue

        page_contexts = [
            f"=== PAGE {pages[i][0]} ==={_two_col_note if two_column_layout else ''}\n{pages[i][1]}"
            for i in range(batch_start, batch_end)
        ]
        first_page = pages[batch_start][0]
        last_page  = pages[batch_end - 1][0]

        try:
            payload  = _extract_batch(page_contexts)
            port     = payload.port.strip() or "Unknown"
            currency = payload.currency.strip()

            for item in payload.fees:
                store.upsert_fee_item(doc_id, port, country, currency, tariff_year, item)

            store.log_batch(doc_id, batch_start, batch_end, "done")
            log.info(f"  PDF pages {first_page}–{last_page}: {len(payload.fees)} fee items  (port={port})")
        except Exception as exc:
            store.log_batch(doc_id, batch_start, batch_end, f"error: {exc}")
            log.error(f"  PDF pages {first_page}–{last_page} failed: {exc}")

        time.sleep(_INTER_BATCH_DELAY)

    status = store.get_ingestion_status(doc_id)
    log.info(
        f"[v1] Pipeline complete — version={version_tag}  {status['total_fee_items']} fee items stored"
        + (f", {skipped} batches skipped" if skipped else "")
        + (f", {status['errors']} batch(es) with errors" if status["errors"] else "")
    )
    return version_tag


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")

    parser = argparse.ArgumentParser(description="Document Preparation Agent v1 — Stage 1 §2.2.1 [DEMO]")
    parser.add_argument("--country",          default="South Africa", help="Country, e.g. 'South Africa', 'UAE'")
    parser.add_argument("--version-tag",      default=None,  help="Version label, e.g. 'FY2025-26-v1'")
    parser.add_argument("--page-start",       type=int, default=1)
    parser.add_argument("--page-end",         type=int, default=None)
    parser.add_argument("--two-column-layout", action="store_true", default=False)
    parser.add_argument("--list-versions",    action="store_true", default=False,
                        help="List all v1 DB versions for the given country and exit.")
    args = parser.parse_args()

    if args.list_versions:
        versions = list_available_versions(args.country)
        if not versions:
            print(f"No v1 versions found for country '{args.country}' in {DB_V1_DIR}")
        for v in versions:
            print(f"  [{v['country']}]  {v['version_tag']}  →  {v['db_path']}")
        raise SystemExit(0)

    pdf_files = sorted(RAW_DIR.glob("*.pdf"))
    if not pdf_files:
        log.error(f"No PDF files found in {RAW_DIR}")
        raise SystemExit(1)
    log.info(f"Found {len(pdf_files)} PDF(s) in {RAW_DIR}")

    for pdf_path in pdf_files:
        tariff_year = _tariff_year_from_name(pdf_path.name)
        version_tag = args.version_tag or tariff_year or pdf_path.stem
        target_db   = db_path_for_version(args.country, version_tag)
        store       = TariffStoreV1(target_db)
        try:
            used_tag = run_document_preparation(
                pdf_path, store,
                country=args.country,
                page_start=args.page_start,
                page_end=args.page_end,
                two_column_layout=args.two_column_layout,
                version_tag=version_tag,
            )
            log.info(f"[v1] DB written: {target_db.name}  version={used_tag}")
        finally:
            store.close()
