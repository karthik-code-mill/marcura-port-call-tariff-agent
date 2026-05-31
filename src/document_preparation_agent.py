"""
Document Preparation Agent  —  Stage 1 §2.2.1
=============================================
Extracts Structured Fee Data from tariff PDFs and persists to SQLite.

No LangChain here — this is a straight extraction pipeline:
  PDF  →  pdfplumber (layout-aware)  →  Gemini SDK (structured JSON)  →  SQLite

LangChain is reserved for the Stage-2 multi-agent execution layer where
agent orchestration, tool routing, and chain composition add real value.

Output store:  context-layer/structured/tariff_store.db
  Table: tariff_fee_items   — one row per (section, fee_item, GT range)
  Table: ingestion_log      — batch-level completion for resumable pipeline

The hierarchical chunk index (§2.2.2) is a separate data source produced
by hierarchical_chunk_agent.py and stored in its own DB.
"""

import json
import logging
import os
import re
import sqlite3
import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from google import genai
from google.genai import types
import pdfplumber
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from tqdm import tqdm

load_dotenv()

# ─── Configuration ────────────────────────────────────────────────────────────

BASE_DIR            = Path(__file__).resolve().parent.parent
RAW_DIR             = BASE_DIR / "context-layer" / "rag" / "raw"
STORE_DIR           = BASE_DIR / "context-layer" / "structured"
DB_PATH             = STORE_DIR / "tariff_store.db"          # legacy / default
PIPELINE_CONFIG_PATH = STORE_DIR / "pipeline_config.json"   # tracks active version


def db_path_for_version(version_tag: str) -> Path:
    """Return the DB path for a named version, e.g. 'FY2025-26' → tariff_store_FY2025-26.db."""
    safe = re.sub(r"[^\w\-.]", "_", version_tag)
    return STORE_DIR / f"tariff_store_{safe}.db"


def save_active_version(version_tag: str) -> None:
    """Persist the active version tag to pipeline_config.json."""
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    config = _load_raw_config()
    if version_tag not in config.get("versions", []):
        config.setdefault("versions", []).append(version_tag)
    config["active_version"] = version_tag
    PIPELINE_CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")


def load_active_version() -> str | None:
    """Return the currently active version tag, or None if no config exists."""
    return _load_raw_config().get("active_version")


def list_available_versions() -> list[dict]:
    """Return all registered versions with their DB paths and active flag."""
    config = _load_raw_config()
    active = config.get("active_version")
    return [
        {
            "version_tag": v,
            "db_path":     str(db_path_for_version(v)),
            "is_active":   v == active,
            "db_exists":   db_path_for_version(v).exists(),
        }
        for v in config.get("versions", [])
    ]


def _load_raw_config() -> dict:
    if PIPELINE_CONFIG_PATH.exists():
        try:
            return json.loads(PIPELINE_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

# Pages per LLM call. 2 pages keeps output JSON well within Gemini's output
# token limit for dense tariff PDFs with many fee bands per section.
BATCH_SIZE = 2

# When a multi-page batch truncates and falls back to per-page extraction,
# this many trailing lines from the previous page are prepended as context
# so cross-page fee sections (header on page N, rates on page N+1) are not lost.
_OVERLAP_LINES = 30

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

STORE_DIR.mkdir(parents=True, exist_ok=True)

# ─── Gemini SDK Setup ─────────────────────────────────────────────────────────

_client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])

# Model cascade: gemini-2.5-flash is tried first (higher quality); when its
# free-tier daily quota (20 RPD) is exhausted the pipeline falls through to
# gemini-2.0-flash (1 500 RPD) automatically for the remainder of the run.
# Note: gemini-1.5-flash is removed from the v1beta endpoint and returns 404.
_MODELS: List[str] = ["gemini-2.5-flash", "gemini-2.0-flash"]
_exhausted_models: set = set()  # models whose daily free-tier quota is gone

# Seconds to sleep between batches.  gemini-2.0-flash allows 15 RPM on the
# free tier; 5 s keeps throughput at ~12 RPM, safely under the limit.
_INTER_BATCH_DELAY = 5

_PROMPTS_DIR = BASE_DIR / "prompts"
_SYSTEM_PROMPT = (_PROMPTS_DIR / "document_preparation_agent_sp_v1.8.md").read_text(encoding="utf-8")

_generation_config = types.GenerateContentConfig(
    system_instruction=_SYSTEM_PROMPT,
    response_mime_type="application/json",
    temperature=0.0,
    max_output_tokens=65536,
)

# ─── Pydantic Output Schema  (§2.2.1 Model v1) ───────────────────────────────

class SurchargeItem(BaseModel):
    condition: str = Field(
        description=(
            "The full trigger condition for this surcharge, copied verbatim from the document. "
            "E.g. 'Outside ordinary working hours (18:00–06:00)', 'Public holidays', "
            "'Vessel cancels after pilot has boarded'. Include any qualifying time windows or thresholds."
        )
    )
    percentage: float = Field(
        description=(
            "The percentage added on top of the base fee when this surcharge applies. "
            "E.g. 50.0 means the total becomes base_fee × 1.50. "
            "Use 0.0 if the surcharge is a flat amount rather than a percentage "
            "(capture the flat amount in the condition text instead)."
        )
    )


class ExceptionItem(BaseModel):
    text: str = Field(
        description=(
            "Verbatim text of the exception clause as it appears in the document. "
            "Never paraphrase. E.g. 'Vessels in ballast are exempt from port dues' or "
            "'A 10% reduction applies to vessels that called at this port within the last 30 days'."
        )
    )
    machine_handled: bool = Field(
        default=False,
        description=(
            "True only if the exception has a deterministic, rule-based outcome the system can "
            "evaluate automatically — e.g. a numeric discount for vessels under 500 GT, a fixed "
            "waiver for ballast voyages. False if the exception requires human judgment, "
            "port-authority discretion, or case-by-case negotiation."
        ),
    )
    source: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Optional source reference for this exception within the document. "
            "E.g. {'section': '3.4', 'page': 12, 'note': 'sub-clause (b)'}."
        ),
    )


class TariffFeeItem(BaseModel):
    section: str = Field(
        description=(
            "Section number exactly as printed in the tariff document, e.g. '3.6', '4.1.2'. "
            "Used as a primary grouping key — copy without modification."
        )
    )
    tariff_fee_item: str = Field(
        description=(
            "Official name of this fee in UPPERCASE exactly as it appears in the document heading, "
            "e.g. 'PILOTAGE SERVICES', 'PORT DUES', 'TOWAGE FEES'. "
            "This is the primary identifier — do not abbreviate or rephrase."
        )
    )
    port: str = Field(
        description=(
            "Port this fee applies to. ONLY three values are valid: "
            "(a) A single normalised port name, spelled identically on every record "
            "in this document, e.g. 'Durban', 'Cape Town', 'Richards Bay', "
            "'Port Elizabeth', 'East London', 'Mossel Bay', 'Saldanha', 'Ngqura'. "
            "(b) 'All' — when the fee applies to all ports or no specific port is "
            "named in the section or header. "
            "(c) 'Other' — ONLY when the document explicitly states the fee covers "
            "ports not listed by name (e.g. 'other ports', 'remaining ports'). "
            "Do NOT use 'Other' as a fallback for uncertainty — use 'All' instead. "
            "NEVER use 'Multiple Ports', 'Various Ports', 'South African Ports', "
            "'National Ports', 'All Ports', or any aggregate label — use 'All' instead. "
            "Derive port from: section heading > page header > document title. "
            "CRITICAL — two cases before deciding the port value: "
            "CASE A (split required): when a fee TABLE has DIFFERENT NUMERIC VALUES for each "
            "named port — whether ports are column headers OR row labels — emit one record per "
            "port using the exact port name; use 'Other' for 'Other Ports'/'Remaining' entries. "
            "Column-oriented example (ports as column headers, as in the real pilotage table): "
            "header row 'Ports | Richards Bay | Durban | Cape Town | Other', "
            "data row 'Per Service | 32864.53 | 19753.04 | 6732.45 | 6950.12' → "
            "emit {port:'Richards Bay', base_fee:32864.53} AND {port:'Durban', base_fee:19753.04} "
            "AND {port:'Cape Town', base_fee:6732.45} AND {port:'Other', base_fee:6950.12} — "
            "one record per column. "
            "Row-oriented example: 'Durban R19 753.04' and 'Other Ports R6 950.12' → "
            "emit {port:'Durban', base_fee:19753.04} AND {port:'Other', base_fee:6950.12}. "
            "Number cleaning: PDF may render digits with spaces ('1 0 2 6 8 . 4 9' = 10268.49) "
            "— remove internal spaces before recording numeric values. "
            "CASE B (no split): a condition sentence listing ports where the fee is compulsory "
            "but showing NO per-port rate difference stays port='All'; store the sentence in "
            "conditions[]. Rule: split ONLY when the table shows DIFFERENT NUMERIC VALUES "
            "per named port — a prose applicability list is never a split signal. "
            "BOTH cases can appear in the SAME section simultaneously: when a section has "
            "a prose applicability sentence (CASE B) AND a per-port rate table (CASE A), "
            "apply CASE A to the table (emit one record per port), copy the prose into "
            "conditions[] on each record, and do NOT collapse the table into port='All'. "
            "NEVER use the 'Other' column value as a catch-all for port='All' — "
            "'Other' means explicitly-unlisted ports only."
        )
    )
    vessel_gt_range: str = Field(
        description=(
            "Gross tonnage band as an explicit numeric 'gt_min-gt_max' string. "
            "BOTH bounds are mandatory — never leave the upper bound implicit. "
            "Normalisation: "
            "'10 001 – 50 000' → '10001-50000'; "
            "'>50 000' / 'above 50 000' / '50 000+' → '50001-999999999'; "
            "'All ranges' / no GT qualifier → '0-999999999'; "
            "'up to 5 000' / '<5 000' → '0-5000'. "
            "Emit one TariffFeeItem per GT band. "
            "Put verbatim source wording in notes if audit trail is needed."
        )
    )
    base_fee: float = Field(
        default=0.0,
        description=(
            "The fixed/base component of the fee in the document's stated currency. "
            "Use 0.0 if the fee is purely formula-driven or explicitly stated as zero. "
            "Do not invent a value — if not stated, use 0.0."
        ),
    )
    incremental_fee_per_100_gt: float = Field(
        default=0.0,
        description=(
            "The additional charge applied per 100 GT (or per stated unit) above the lower GT bound. "
            "Use 0.0 if the fee has no incremental component. "
            "E.g. if the tariff says '+R 12.50 per 100 GT above 10 000 GT', this is 12.50."
        ),
    )
    formula: str = Field(
        default="",
        description=(
            "Python-evaluable expression for computing the total fee. "
            "Available variables: GT (vessel gross tonnage as float), base_fee, increment, "
            "lower_bound, upper_bound. "
            "E.g. 'base_fee + (math.ceil((GT - lower_bound) / 100) * increment)'. "
            "Leave empty string if the fee is a flat base_fee with no further calculation needed. "
            "Use the exact sentinel string 'EXTERNAL_DETERMINATION_REFERENCED_SKIP' for "
            "pass-through / statutory fees that reference external legislation (see Rule 12)."
        ),
    )
    conditions: List[str] = Field(
        default_factory=list,
        description=(
            "List of conditions or applicability rules copied verbatim from the document. "
            "These govern WHEN or HOW the fee applies — not special discounts or exemptions "
            "(those are exceptions). "
            "E.g. ['Applies to all vessels berthing at the container terminal', "
            "'Minimum charge: R 500', 'Charged per commenced hour']."
        ),
    )
    surcharges: List[SurchargeItem] = Field(
        default_factory=list,
        description=(
            "List of percentage-based add-ons that increase the base fee under specific conditions "
            "such as after-hours work, public holidays, or emergency call-outs. "
            "Each surcharge has a trigger condition and a percentage value."
        ),
    )
    exceptions: List[ExceptionItem] = Field(
        default_factory=list,
        description=(
            "List of exception clauses — special cases where the standard fee is modified, "
            "reduced, waived, or replaced by an alternative rate. "
            "Copy clause text verbatim. E.g. vessels in ballast, special port agreements, "
            "vessels below a minimum GT threshold."
        ),
    )
    notes: Dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Catch-all dictionary for any qualifying text that does not fit conditions, "
            "surcharges, or exceptions. Use descriptive keys. "
            "E.g. {'payment_terms': 'Invoiced monthly, payable within 30 days', "
            "'currency_note': 'Fees stated in ZAR excluding VAT', "
            "'definition': 'GT means gross tonnage per international tonnage certificate', "
            "'reference': 'Refer to Section 1.2 for general definitions'}. "
            "When in doubt, place content here rather than discarding it."
        ),
    )
    unmodeled_clauses: List[str] = Field(
        default_factory=list,
        description=(
            "Verbatim text of clauses that are COMPLETELY UNPROCESSABLE without human authority "
            "or external context — reserved exclusively for: "
            "(a) clauses requiring port-authority or government discretion "
            "    ('as agreed with the port master', 'subject to customs officer approval'), "
            "(b) purely procedural instructions addressed to human officials, "
            "(c) references to external regulations that cannot be interpreted from this document. "
            "Do NOT use this for fees with unclear conditions or amounts — those belong in notes. "
            "Anything placed here will be flagged for mandatory human review."
        ),
    )
    extraction_confidence: float = Field(
        default=1.0,
        description=(
            "Confidence score 0.0–1.0 reflecting how clearly the fee type and conditions "
            "could be identified from the source text: "
            "1.0 = All fields explicitly and unambiguously stated in the document. "
            "0.8 = Minor ambiguity in one field (e.g. GT range phrasing, currency not stated). "
            "0.6 = Some fields inferred from surrounding context; core fee data is sound. "
            "0.4 = Fee type unclear OR conditions largely inferred — review recommended. "
            "0.0–0.2 = Could not reliably determine fee type AND/OR conditions — human review required. "
            "IMPORTANT: placing content in notes does NOT lower confidence. "
            "Only lower the score when the fee type or its conditions are genuinely uncertain."
        ),
    )
    source_page: int = Field(
        default=0,
        description="1-indexed page number in the PDF where this fee item first appears.",
    )


class PortTariffPayload(BaseModel):
    port: str = Field(
        description=(
            "Name of the port this tariff document covers, extracted from the document title, "
            "header, or introductory section. E.g. 'Durban', 'Richards Bay'."
        )
    )
    country: str = Field(
        default="",
        description="Country where the port is located, if stated in the document. E.g. 'South Africa'.",
    )
    currency: str = Field(
        default="",
        description="Currency code or symbol used for all fees in this document, e.g. 'ZAR', 'USD', 'R'.",
    )
    fees:     List[TariffFeeItem] = Field(default_factory=list)


# ─── LLM Call with Retry ─────────────────────────────────────────────────────

def _parse_retry_delay(exc: Exception) -> int | None:
    """Extract the API-suggested retry delay (seconds) from a 429 error string."""
    m = re.search(r"['\"]retryDelay['\"]\s*:\s*['\"](\d+)s['\"]", str(exc))
    return int(m.group(1)) if m else None


def _is_daily_quota(exc: Exception) -> bool:
    """True when the 429 is a daily-limit exhaustion, not a transient RPM burst."""
    return "PerDay" in str(exc)


def _extract_batch(page_contexts: List[str], max_retries: int = 3) -> PortTariffPayload:
    """
    Call Gemini for a list of assembled page-context strings and return a
    validated PortTariffPayload.

    Model cascade: tries each model in _MODELS in order.  When a model's daily
    free-tier quota is exhausted (429 + PerDay in the error) it is added to
    _exhausted_models and the next model is attempted immediately.  Transient
    RPM 429s are retried with exponential back-off on the same model.

    On truncation (EOF while parsing a string): the same multi-page payload
    will fail again on retry, so the function splits the batch into individual
    single-page calls, merges results, and returns the combined payload.
    """
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
        raise RuntimeError(
            "All models in the cascade have exhausted their daily free-tier quota"
        )

    for model in available:
        for attempt in range(max_retries):
            try:
                response = _client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=_generation_config,
                )
                return PortTariffPayload.model_validate_json(response.text)
            except Exception as exc:
                err_str = str(exc)

                # Truncated JSON — retrying the same multi-page payload will
                # produce the same truncation.  Fall back to per-page calls.
                #
                # Cross-page continuity: fee sections sometimes start near the
                # bottom of page N and continue onto page N+1.  To avoid losing
                # context, each page (except the first) is prefixed with the last
                # _OVERLAP_LINES lines of the preceding page.  This tail is small
                # enough not to re-trigger truncation on a single-page call.
                if "EOF while parsing" in err_str and len(page_contexts) > 1:
                    log.warning(
                        f"Truncated JSON on {len(page_contexts)}-page batch — "
                        "falling back to per-page extraction with overlap"
                    )
                    merged    = PortTariffPayload(port="", country="", currency="", fees=[])
                    prev_tail = ""
                    for page_ctx in page_contexts:
                        if prev_tail:
                            ctx = (
                                "[PRIOR PAGE CONTEXT — for continuity only; "
                                "do not re-extract fees already on that page]\n"
                                f"{prev_tail}\n"
                                "[END PRIOR PAGE CONTEXT]\n\n"
                                f"{page_ctx}"
                            )
                        else:
                            ctx = page_ctx
                        lines = page_ctx.splitlines()
                        prev_tail = "\n".join(lines[-_OVERLAP_LINES:])
                        try:
                            result = _extract_batch([ctx], max_retries=max_retries)
                            if not merged.port     and result.port:     merged.port     = result.port
                            if not merged.country  and result.country:  merged.country  = result.country
                            if not merged.currency and result.currency: merged.currency = result.currency
                            merged.fees.extend(result.fees)
                        except Exception as page_exc:
                            log.error(f"  Per-page fallback failed: {page_exc}")
                    return merged

                # Daily quota exhausted for this model → cascade to the next one.
                if "RESOURCE_EXHAUSTED" in err_str and _is_daily_quota(exc):
                    _exhausted_models.add(model)
                    remaining = [m for m in _MODELS if m not in _exhausted_models]
                    log.warning(
                        f"Daily quota exhausted for {model} — "
                        f"cascading to: {remaining[0] if remaining else 'none (all exhausted)'}"
                    )
                    break  # stop retrying this model; outer loop tries the next one

                if attempt == max_retries - 1:
                    raise
                # Honour the API's suggested retry delay for transient 429s;
                # fall back to exponential backoff for other transient errors.
                suggested = _parse_retry_delay(exc)
                wait = suggested if suggested else 2 ** attempt
                log.warning(
                    f"LLM call failed ({model}, attempt {attempt + 1}): {exc} — retrying in {wait}s"
                )
                time.sleep(wait)

    raise RuntimeError(
        "All models in the cascade have exhausted their daily free-tier quota"
    )


# ─── GT Range Parser ──────────────────────────────────────────────────────────

def parse_gt_range(gt_range: str) -> Tuple[float, float]:
    """Parse a GT range string into (gt_min, gt_max) for indexed SQL queries."""
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


# ─── SQLite Persistence ───────────────────────────────────────────────────────

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


class TariffStore:
    """
    Persistence layer for §2.2.1 Structured Fee Data.

    Primary query (§2.3.1 Retriever Agent):
        WHERE port = ? AND gt_min <= ? AND gt_max >= ?
    """

    def __init__(self, db_path: Path) -> None:
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA_SQL)
        self.conn.commit()
        log.info(f"TariffStore ready: {db_path}")

    def upsert_fee_item(
        self,
        doc_id: str,
        port: str,
        country: str,
        currency: str,
        tariff_year: str,
        item: TariffFeeItem,
    ) -> None:
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
                item.extraction_confidence,
                item.source_page,
                now,
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

    def query_fees(self, port: str, gt: float) -> List[dict]:
        """Stage-2 Retriever entry point. Returns all fee items for port + GT."""
        rows = self.conn.execute(
            """
            SELECT * FROM tariff_fee_items
            WHERE port = ? AND gt_min <= ? AND gt_max >= ?
            ORDER BY section
            """,
            (port, gt, gt),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_ingestion_status(self, doc_id: str) -> dict:
        rows = self.conn.execute(
            "SELECT * FROM ingestion_log WHERE doc_id=? ORDER BY batch_start",
            (doc_id,),
        ).fetchall()
        total_fees = self.conn.execute(
            "SELECT COUNT(*) FROM tariff_fee_items WHERE doc_id=?", (doc_id,)
        ).fetchone()[0]
        batches = [dict(r) for r in rows]
        done    = sum(1 for b in batches if b["status"] == "done")
        errors  = sum(1 for b in batches if b["status"].startswith("error"))
        return {
            "doc_id": doc_id,
            "total_batches": len(batches),
            "done": done,
            "errors": errors,
            "total_fee_items": total_fees,
            "batches": batches,
        }

    def close(self) -> None:
        self.conn.close()


# ─── PDF Loading ──────────────────────────────────────────────────────────────

def load_pdf_pages(
    pdf_path: Path,
    page_start: int = 1,
    page_end: int | None = None,
) -> List[Tuple[int, str]]:
    """
    Extract per-page text with layout awareness via pdfplumber.
    Table cells are appended after prose text so the LLM sees both
    the narrative context and the structured rate grid in one block.

    page_start / page_end are 1-indexed and inclusive.  Only pages in that
    range are returned; pages outside it (e.g. cover, TOC, acronyms) are
    skipped so the LLM never sees non-fee content.

    Returns a list of (pdf_page_number, text) tuples so the real page number
    is preserved regardless of the slice offset.
    """
    pages: List[Tuple[int, str]] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        total   = len(pdf.pages)
        end_idx = min((page_end or total), total)          # 1-indexed end, clamped
        start_idx = max(0, page_start - 1)                  # convert to 0-indexed
        for pdf_page in pdf.pages[start_idx:end_idx]:
            page_num = pdf_page.page_number                 # pdfplumber: 1-indexed
            plain    = pdf_page.extract_text(x_tolerance=3, y_tolerance=3) or ""
            tables   = pdf_page.extract_tables()
            if tables:
                table_blocks = []
                for tbl in tables:
                    rows = [
                        "\t".join((cell or "").strip() for cell in row)
                        for row in tbl if any(row)
                    ]
                    table_blocks.append("\n".join(rows))
                pages.append((page_num, plain + "\n\n[TABLE]\n" + "\n\n[TABLE]\n".join(table_blocks)))
            else:
                pages.append((page_num, plain))
    return pages


# ─── Helpers ──────────────────────────────────────────────────────────────────

def doc_id_from_path(
    pdf_path: Path,
    page_start: int = 1,
    page_end: int | None = None,
) -> str:
    # Include page range so different slices of the same PDF get separate
    # ingestion-log entries and do not interfere with each other's batch state.
    key = f"{pdf_path.name}:{page_start}:{page_end or 'end'}"
    return hashlib.md5(key.encode()).hexdigest()[:12]


def tariff_year_from_name(name: str) -> str:
    m = re.search(r"FY[\-_]?(\d{4}[\-_]\d{2,4}|\d{4})", name, re.IGNORECASE)
    return m.group(1) if m else ""


# ─── Pipeline ─────────────────────────────────────────────────────────────────

def run_document_preparation(
    pdf_path: Path,
    store: TariffStore,
    page_start: int = 1,
    page_end: int | None = None,
    two_column_layout: bool = False,
    version_tag: str | None = None,
) -> str:
    """
    Full Stage-1 §2.2.1 pipeline for one PDF:
      1. Load pages (optionally a sub-range) with layout-aware text extraction.
      2. Iterate page batches — skip already-completed batches (resumable).
      3. Call Gemini for structured fee extraction per batch.
      4. Persist each TariffFeeItem to tariff_fee_items.
      5. Log batch result; errors are recorded but do not abort the run.
      6. Save the version tag as active in pipeline_config.json.

    page_start / page_end  — 1-indexed, inclusive.  Use to skip front-matter
        (cover, TOC, abbreviations) that contains no fee data.
    two_column_layout      — Set True when each PDF page carries two side-by-side
        document pages (scanned booklet layout).  A hint is injected into each
        page header so the LLM reads both columns as one logical page.
    version_tag            — Human-readable label, e.g. 'FY2025-26'.  Auto-derived
        from the PDF filename when not provided.  This label is saved to
        pipeline_config.json so the retriever/orchestrator know which DB to open.

    Returns the version_tag used (useful when auto-generated).
    """
    doc_id      = doc_id_from_path(pdf_path, page_start, page_end)
    tariff_year = tariff_year_from_name(pdf_path.name)

    if version_tag is None:
        version_tag = tariff_year or hashlib.md5(
            f"{pdf_path.name}:{doc_id}".encode()
        ).hexdigest()[:8]

    range_desc = f"pages {page_start}–{page_end or 'end'}"
    log.info(
        f"Pipeline start: {pdf_path.name}  doc_id={doc_id}  "
        f"version={version_tag}  range={range_desc}  two_column={two_column_layout}"
    )
    pages = load_pdf_pages(pdf_path, page_start=page_start, page_end=page_end)
    total = len(pages)
    log.info(f"Loaded {total} pages ({range_desc})")

    _two_col_note = (
        " [TWO-COLUMN LAYOUT: this PDF page contains two side-by-side document "
        "pages. Read both columns left-to-right as continuous fee content.]"
    )

    skipped = 0
    for batch_start in tqdm(range(0, total, BATCH_SIZE), desc=pdf_path.stem, unit="batch"):
        batch_end = min(batch_start + BATCH_SIZE, total)

        if store.is_batch_done(doc_id, batch_start, batch_end):
            skipped += 1
            continue

        page_contexts = []
        for i in range(batch_start, batch_end):
            page_num, text = pages[i]
            header = f"=== PAGE {page_num} ===" + (_two_col_note if two_column_layout else "")
            page_contexts.append(f"{header}\n{text}")

        # Use real PDF page numbers in the progress log
        first_page = pages[batch_start][0]
        last_page  = pages[batch_end - 1][0]

        try:
            payload = _extract_batch(page_contexts)
            port     = payload.port.strip() or "Unknown"
            country  = payload.country.strip()
            currency = payload.currency.strip()

            for item in payload.fees:
                store.upsert_fee_item(doc_id, port, country, currency, tariff_year, item)

            store.log_batch(doc_id, batch_start, batch_end, "done")
            log.info(
                f"  PDF pages {first_page}–{last_page}: "
                f"{len(payload.fees)} fee items  (port={port})"
            )
        except Exception as exc:
            store.log_batch(doc_id, batch_start, batch_end, f"error: {exc}")
            log.error(f"  PDF pages {first_page}–{last_page} failed: {exc}")

        # Throttle to stay under the free-tier RPM cap of whichever model
        # is currently active (gemini-1.5-flash: 15 RPM; 2.5-flash: 10 RPM).
        time.sleep(_INTER_BATCH_DELAY)

    status = store.get_ingestion_status(doc_id)
    save_active_version(version_tag)
    log.info(
        f"Pipeline complete — version={version_tag}  "
        f"{status['total_fee_items']} fee items stored"
        + (f", {skipped} batches skipped (already done)" if skipped else "")
        + (f", {status['errors']} batch(es) with errors" if status["errors"] else "")
    )
    return version_tag


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Document Preparation Agent — Stage 1 §2.2.1",
    )
    parser.add_argument(
        "--page-start",
        type=int,
        default=1,
        metavar="N",
        help="First PDF page to process, 1-indexed (default: 1). "
             "Use to skip cover/TOC/acronym pages that contain no fee data.",
    )
    parser.add_argument(
        "--page-end",
        type=int,
        default=None,
        metavar="N",
        help="Last PDF page to process, 1-indexed inclusive (default: last page).",
    )
    parser.add_argument(
        "--two-column-layout",
        action="store_true",
        default=False,
        help="Each PDF page is a 2-column scan (two logical document pages side by side). "
             "A hint is injected into every page header so the LLM reads both columns "
             "as continuous fee content. Leave unset for single-page-per-page PDFs.",
    )
    parser.add_argument(
        "--version-tag",
        default=None,
        metavar="TAG",
        help="Human-readable version label, e.g. 'FY2025-26'. "
             "Data is stored in tariff_store_<TAG>.db and saved as the active version "
             "in pipeline_config.json so the retriever/orchestrator pick it up automatically. "
             "Defaults to the fiscal year extracted from the PDF filename.",
    )
    parser.add_argument(
        "--list-versions",
        action="store_true",
        default=False,
        help="List all available tariff versions and exit.",
    )
    args = parser.parse_args()

    if args.list_versions:
        versions = list_available_versions()
        if not versions:
            print("No versions found.")
        for v in versions:
            active_marker = " ← active" if v["is_active"] else ""
            exists_marker = "" if v["db_exists"] else " [DB missing]"
            print(f"  {v['version_tag']}{active_marker}{exists_marker}  →  {v['db_path']}")
        raise SystemExit(0)

    target_db = db_path_for_version(args.version_tag) if args.version_tag else DB_PATH
    store = TariffStore(target_db)
    try:
        pdf_files = sorted(RAW_DIR.glob("*.pdf"))
        if not pdf_files:
            log.error(f"No PDF files found in {RAW_DIR}")
        else:
            log.info(f"Found {len(pdf_files)} PDF(s) in {RAW_DIR}")
            for pdf_path in pdf_files:
                used_tag = run_document_preparation(
                    pdf_path,
                    store,
                    page_start=args.page_start,
                    page_end=args.page_end,
                    two_column_layout=args.two_column_layout,
                    version_tag=args.version_tag,
                )
                log.info(f"Active version set to: {used_tag}")
    finally:
        store.close()
