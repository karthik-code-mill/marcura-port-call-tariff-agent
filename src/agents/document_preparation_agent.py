"""
Document Preparation Agent — Shared Foundation
===============================================
This module provides shared infrastructure used by the three specialised
document preparation agents (parser, rule_extractor, validation) and by the
Stage-2 retriever pipeline.

Contents:
  - Path constants (RAW_DIR, OUT_DIR, DB_DIR, CONFIG_PATH)
  - Naming helpers (db_path_for_version, md_path_for)
  - Multi-country app config (save_active_version, load_active_version, etc.)
  - TariffStore — SQLite persistence layer shared across agents
  - Utilities: parse_gt_range, _doc_id, _tariff_year_from_name

PDF conversion and LLM extraction logic has been moved to the dedicated agents:
  - agents/parser_agent.py        (PDF → Markdown)
  - agents/rule_extractor_agent.py (Markdown → DB fee rules)
  - agents/validation_agent.py    (validation pass)
  - orchestrator/document_prep_pipeline.py (LangGraph orchestration)
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# ─── Paths ────────────────────────────────────────────────────────────────────

BASE_DIR    = Path(__file__).resolve().parent.parent.parent
RAW_DIR     = BASE_DIR / "context-layer" / "rag" / "raw"
OUT_DIR     = BASE_DIR / "context-layer" / "rag" / "out"
DB_DIR      = BASE_DIR / "context-layer" / "rag" / "db"
CONFIG_PATH = BASE_DIR / "config" / "app_config.json"

OUT_DIR.mkdir(parents=True, exist_ok=True)
DB_DIR.mkdir(parents=True, exist_ok=True)


# ─── Naming helpers ───────────────────────────────────────────────────────────

def _country_slug(country: str) -> str:
    return re.sub(r"\s+", "-", country.strip().lower())


def db_path_for_version(country: str, version_tag: str) -> Path:
    safe = re.sub(r"[^\w\-.]", "_", version_tag)
    return DB_DIR / f"{_country_slug(country)}-tariff-store-{safe}.db"


def md_path_for(country: str, version_tag: str) -> Path:
    safe = re.sub(r"[^\w\-.]", "_", version_tag)
    return OUT_DIR / f"{_country_slug(country)}-tariff-book-{safe}.md"


def raw_dir_for(country: str) -> Path:
    """Country-specific subdirectory under RAW_DIR for input PDFs.

    Mirrors the same slug convention used by db_path_for_version and md_path_for
    so every country's data (input and output) lives under its own slug.

    The directory is created on first call so callers never need to mkdir manually.
    """
    d = RAW_DIR / _country_slug(country)
    d.mkdir(parents=True, exist_ok=True)
    return d


# ─── Multi-country app config ─────────────────────────────────────────────────

def _load_app_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"default_country": "", "countries": {}}


def _save_app_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def save_active_version(country: str, version_tag: str) -> None:
    cfg   = _load_app_config()
    entry = cfg.setdefault("countries", {}).setdefault(country, {"active_version": "", "versions": []})
    if version_tag not in entry["versions"]:
        entry["versions"].append(version_tag)
    entry["active_version"] = version_tag
    if not cfg.get("default_country"):
        cfg["default_country"] = country
    _save_app_config(cfg)


def load_active_version(country: str) -> Optional[str]:
    return _load_app_config().get("countries", {}).get(country, {}).get("active_version") or None


def list_available_versions(country: str = "") -> List[dict]:
    cfg      = _load_app_config()
    results  = []
    countries = [country] if country else list(cfg.get("countries", {}).keys())
    for c in countries:
        entry  = cfg.get("countries", {}).get(c, {})
        active = entry.get("active_version", "")
        for v in entry.get("versions", []):
            path = db_path_for_version(c, v)
            results.append({
                "country":     c,
                "version_tag": v,
                "db_path":     str(path),
                "is_active":   v == active,
                "db_exists":   path.exists(),
            })
    return results


# ─── Extraction models (canonical location: models/tariff_extraction.py) ──────

from models.tariff_extraction import ExceptionItem, PortTariffPayload, SurchargeItem, TariffFeeItem  # noqa: E402


# ─── SQLite persistence ───────────────────────────────────────────────────────

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tariff_fee_items (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id                      TEXT    NOT NULL,
    port                        TEXT    NOT NULL,
    country                     TEXT    DEFAULT '',
    currency                    TEXT    DEFAULT '',
    tariff_year                 TEXT    DEFAULT '',
    section                     TEXT    NOT NULL,
    section_name                TEXT    DEFAULT '',
    tariff_fee_item             TEXT    NOT NULL,
    vessel_type                 TEXT    DEFAULT 'All',
    vessel_gt_range             TEXT    DEFAULT 'All ranges',
    gt_min                      REAL    DEFAULT 0,
    gt_max                      REAL    DEFAULT 999999999,
    base_fee                    REAL    DEFAULT 0,
    incremental_fee_per_100_gt  REAL    DEFAULT 0,
    formula                     TEXT    DEFAULT '',
    port_condition              TEXT    DEFAULT '',
    conditions                  TEXT    DEFAULT '[]',
    surcharges                  TEXT    DEFAULT '[]',
    exceptions                  TEXT    DEFAULT '[]',
    notes                       TEXT    DEFAULT '{}',
    unmodeled_clauses           TEXT    DEFAULT '[]',
    extraction_confidence       REAL    DEFAULT 1.0,
    source_page                 INTEGER DEFAULT 0,
    ingested_at                 TEXT    NOT NULL,
    UNIQUE(doc_id, port, section, tariff_fee_item, vessel_type, vessel_gt_range)
);

CREATE TABLE IF NOT EXISTS ingestion_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id      TEXT    NOT NULL,
    section_id  TEXT    NOT NULL,
    status      TEXT    NOT NULL,
    logged_at   TEXT    NOT NULL,
    UNIQUE(doc_id, section_id)
);

CREATE INDEX IF NOT EXISTS idx_fee_port       ON tariff_fee_items(port);
CREATE INDEX IF NOT EXISTS idx_fee_gt         ON tariff_fee_items(gt_min, gt_max);
CREATE INDEX IF NOT EXISTS idx_fee_section    ON tariff_fee_items(section);
CREATE INDEX IF NOT EXISTS idx_fee_confidence ON tariff_fee_items(extraction_confidence);
"""


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


class TariffStore:
    """
    SQLite persistence layer for structured tariff fee data.
    Used by: rule_extractor_agent (write), retriever_agent (read), validation_agent (read/update).
    """

    def __init__(self, db_path: Path) -> None:
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA_SQL)
        self.conn.commit()
        # Migrate existing DBs — add port_condition if the column is absent.
        # ALTER TABLE ADD COLUMN is idempotent via the except: existing DBs silently skip.
        try:
            self.conn.execute("ALTER TABLE tariff_fee_items ADD COLUMN port_condition TEXT DEFAULT ''")
            self.conn.commit()
            log.debug(f"TariffStore: migrated port_condition column in {db_path.name}")
        except Exception:
            pass  # column already present
        log.info(f"TariffStore ready: {db_path}")

    def upsert_fee_item(self, doc_id: str, country: str, currency: str, tariff_year: str, item: TariffFeeItem) -> None:
        gt_min, gt_max = parse_gt_range(item.vessel_gt_range)
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            INSERT INTO tariff_fee_items
                (doc_id, port, country, currency, tariff_year,
                 section, section_name, tariff_fee_item,
                 vessel_type, vessel_gt_range, gt_min, gt_max,
                 base_fee, incremental_fee_per_100_gt, formula,
                 port_condition, conditions, surcharges, exceptions, notes,
                 unmodeled_clauses, extraction_confidence, source_page, ingested_at)
            VALUES (?,?,?,?,?, ?,?,?, ?,?,?,?, ?,?,?, ?,?,?,?,?, ?,?,?,?)
            ON CONFLICT(doc_id, port, section, tariff_fee_item, vessel_type, vessel_gt_range)
            DO UPDATE SET
                section_name               = excluded.section_name,
                vessel_type                = excluded.vessel_type,
                base_fee                   = excluded.base_fee,
                incremental_fee_per_100_gt = excluded.incremental_fee_per_100_gt,
                gt_min                     = excluded.gt_min,
                gt_max                     = excluded.gt_max,
                formula                    = excluded.formula,
                port_condition             = excluded.port_condition,
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
                doc_id, item.port, country, currency, tariff_year,
                item.section, item.section_name, item.tariff_fee_item,
                item.vessel_type, item.vessel_gt_range, gt_min, gt_max,
                item.base_fee, item.incremental_fee_per_100_gt, item.formula,
                item.port_condition,
                json.dumps(item.conditions),
                json.dumps([s.model_dump() for s in item.surcharges]),
                json.dumps([e.model_dump() for e in item.exceptions]),
                json.dumps(item.notes),
                json.dumps(item.unmodeled_clauses),
                item.extraction_confidence, item.source_page, now,
            ),
        )
        self.conn.commit()

    def log_section(self, doc_id: str, section_id: str, status: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            INSERT INTO ingestion_log (doc_id, section_id, status, logged_at)
            VALUES (?,?,?,?)
            ON CONFLICT(doc_id, section_id)
            DO UPDATE SET status = excluded.status, logged_at = excluded.logged_at
            """,
            (doc_id, section_id, status, now),
        )
        self.conn.commit()

    def is_section_done(self, doc_id: str, section_id: str) -> bool:
        row = self.conn.execute(
            "SELECT status FROM ingestion_log WHERE doc_id=? AND section_id=?",
            (doc_id, section_id),
        ).fetchone()
        return row is not None and row["status"] == "done"

    def get_ingestion_status(self, doc_id: str) -> dict:
        rows = self.conn.execute(
            "SELECT * FROM ingestion_log WHERE doc_id=? ORDER BY section_id", (doc_id,)
        ).fetchall()
        total_fees = self.conn.execute(
            "SELECT COUNT(*) FROM tariff_fee_items WHERE doc_id=?", (doc_id,)
        ).fetchone()[0]
        batches = [dict(r) for r in rows]
        return {
            "doc_id":          doc_id,
            "total_sections":  len(batches),
            "done":            sum(1 for b in batches if b["status"] == "done"),
            "errors":          sum(1 for b in batches if b["status"].startswith("error")),
            "total_fee_items": total_fees,
        }

    def close(self) -> None:
        self.conn.close()


# ─── Utilities used by orchestrator and pipeline ──────────────────────────────

def _tariff_year_from_name(name: str) -> str:
    m = re.search(r"FY[\-_]?(\d{4}[\-_]\d{2,4}|\d{4})", name, re.IGNORECASE)
    return m.group(1) if m else ""


def _doc_id(pdf_path: Path, page_start: int, page_end: Optional[int]) -> str:
    key = f"v2:{pdf_path.name}:{page_start}:{page_end or 'end'}"
    return hashlib.md5(key.encode()).hexdigest()[:12]
