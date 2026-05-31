"""
Document Preparation Agent — Two-Pass PDF Extraction Pipeline
=============================================================
Pass 1: PDF → Markdown
Pass 2: Markdown → Structured Fee Data (SQLite)

CLI:
    python -m agents.document_preparation_agent --pass all --country "South Africa" --version-tag FY2025-26-v3.2
    python -m agents.document_preparation_agent --pass 1 --country "South Africa" --version-tag FY2025-26-v3.2
    python -m agents.document_preparation_agent --pass 2 --country "South Africa" --version-tag FY2025-26-v3.2 --md-path <path>
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import fitz
import pymupdf4llm
from dotenv import load_dotenv
from google import genai
from google.genai import types
from tqdm import tqdm

load_dotenv()

log = logging.getLogger(__name__)

# ─── Paths ────────────────────────────────────────────────────────────────────

BASE_DIR    = Path(__file__).resolve().parent.parent.parent
RAW_DIR     = BASE_DIR / "context-layer" / "rag" / "raw"
OUT_DIR     = BASE_DIR / "context-layer" / "rag" / "out"
DB_DIR      = BASE_DIR / "context-layer" / "rag" / "db"
CONFIG_PATH = BASE_DIR / "context-layer" / "config" / "app_config.json"

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


# ─── App config (multi-country) ───────────────────────────────────────────────

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
    cfg = _load_app_config()
    entry = cfg.setdefault("countries", {}).setdefault(country, {"active_version": "", "versions": []})
    if version_tag not in entry["versions"]:
        entry["versions"].append(version_tag)
    entry["active_version"] = version_tag
    if not cfg.get("default_country"):
        cfg["default_country"] = country
    _save_app_config(cfg)


def load_active_version(country: str) -> str | None:
    cfg = _load_app_config()
    return cfg.get("countries", {}).get(country, {}).get("active_version") or None


def list_available_versions(country: str = "") -> list[dict]:
    cfg = _load_app_config()
    results = []
    countries = [country] if country else list(cfg.get("countries", {}).keys())
    for c in countries:
        entry = cfg.get("countries", {}).get(c, {})
        active = entry.get("active_version", "")
        for v in entry.get("versions", []):
            path = db_path_for_version(c, v)
            results.append({
                "country":    c,
                "version_tag": v,
                "db_path":    str(path),
                "is_active":  v == active,
                "db_exists":  path.exists(),
            })
    return results


# ─── Extraction models (defined in models/tariff_extraction.py) ───────────────

from models.tariff_extraction import SurchargeItem, ExceptionItem, TariffFeeItem, PortTariffPayload  # noqa: E402


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
    conditions                  TEXT    DEFAULT '[]',
    surcharges                  TEXT    DEFAULT '[]',
    exceptions                  TEXT    DEFAULT '[]',
    notes                       TEXT    DEFAULT '{}',
    unmodeled_clauses           TEXT    DEFAULT '[]',
    extraction_confidence       REAL    DEFAULT 1.0,
    source_page                 INTEGER DEFAULT 0,
    ingested_at                 TEXT    NOT NULL,
    UNIQUE(doc_id, section, tariff_fee_item, vessel_type, vessel_gt_range)
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
    def __init__(self, db_path: Path) -> None:
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA_SQL)
        self.conn.commit()
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
                 conditions, surcharges, exceptions, notes,
                 unmodeled_clauses, extraction_confidence, source_page, ingested_at)
            VALUES (?,?,?,?,?, ?,?,?, ?,?,?,?, ?,?,?, ?,?,?,?, ?,?,?,?)
            ON CONFLICT(doc_id, section, tariff_fee_item, vessel_type, vessel_gt_range)
            DO UPDATE SET
                section_name               = excluded.section_name,
                vessel_type                = excluded.vessel_type,
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
                doc_id, item.port, country, currency, tariff_year,
                item.section, item.section_name, item.tariff_fee_item,
                item.vessel_type, item.vessel_gt_range, gt_min, gt_max,
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
            "doc_id": doc_id,
            "total_sections": len(batches),
            "done":  sum(1 for b in batches if b["status"] == "done"),
            "errors": sum(1 for b in batches if b["status"].startswith("error")),
            "total_fee_items": total_fees,
        }

    def close(self) -> None:
        self.conn.close()


# ─── Gemini setup ─────────────────────────────────────────────────────────────

_client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
_MODELS: List[str] = ["gemini-2.5-flash", "gemini-2.0-flash"]
_exhausted_models: set = set()
_INTER_BATCH_DELAY = 5

_PROMPTS_DIR = BASE_DIR / "prompts"
_SP_PATH     = _PROMPTS_DIR / "document_preparation_agent_sp_v2.0.md"
_SYSTEM_PROMPT = (
    _SP_PATH.read_text(encoding="utf-8")
    if _SP_PATH.exists()
    else (_PROMPTS_DIR / "document_preparation_agent_sp_v1.8.md").read_text(encoding="utf-8")
)

_generation_config = types.GenerateContentConfig(
    system_instruction=_SYSTEM_PROMPT,
    response_mime_type="application/json",
    temperature=0.0,
    max_output_tokens=65536,
)


# ─── LLM call ─────────────────────────────────────────────────────────────────

def _parse_retry_delay(exc: Exception) -> int | None:
    m = re.search(r"['\"]retryDelay['\"]\s*:\s*['\"](\d+)s['\"]", str(exc))
    return int(m.group(1)) if m else None


def _is_daily_quota(exc: Exception) -> bool:
    return "PerDay" in str(exc)


def _extract_section_fees(section_markdown: str, section_number: str, section_title: str, max_retries: int = 3) -> PortTariffPayload:
    schema = PortTariffPayload.model_json_schema()
    prompt = (
        f"Extract all tariff fee items from the section below.\n\n"
        f"## Section Reference\nSection: {section_number or 'N/A'}  Heading: {section_title}\n\n"
        f"## Schema\n{json.dumps(schema, indent=2)}\n\n"
        f"## Section Markdown\n\n{section_markdown}"
    )

    available = [m for m in _MODELS if m not in _exhausted_models]
    if not available:
        raise RuntimeError("All models exhausted their daily quota")

    for model in available:
        for attempt in range(max_retries):
            try:
                response = _client.models.generate_content(model=model, contents=prompt, config=_generation_config)
                return PortTariffPayload.model_validate_json(response.text)
            except Exception as exc:
                err_str = str(exc)
                if "RESOURCE_EXHAUSTED" in err_str and _is_daily_quota(exc):
                    _exhausted_models.add(model)
                    break
                if attempt == max_retries - 1:
                    raise
                suggested = _parse_retry_delay(exc)
                wait = suggested if suggested else 2 ** attempt
                log.warning(f"  LLM call failed ({model}, attempt {attempt+1}): {exc} — retry in {wait}s")
                time.sleep(wait)

    raise RuntimeError("All models exhausted their daily quota")


# ─── Pass 1 ───────────────────────────────────────────────────────────────────

def convert_pdf_to_markdown(pdf_path: Path, page_start: int = 1, page_end: Optional[int] = None, two_column_layout: bool = False) -> str:
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


def run_pass1(
    pdf_path: Path,
    page_start: int = 1,
    page_end: Optional[int] = None,
    two_column_layout: bool = False,
    country: str = "",
    version_tag: str = "",
    output_dir: Path = OUT_DIR,
) -> Path:
    log.info(f"Pass 1 start: {pdf_path.name}  pages {page_start}–{page_end or 'end'}")

    if country and version_tag:
        out_path = md_path_for(country, version_tag)
    else:
        out_path = output_dir / f"{pdf_path.stem}_v2.md"

    log.info("  [A] Converting PDF to Markdown...")
    md = convert_pdf_to_markdown(pdf_path, page_start, page_end, two_column_layout)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    log.info(f"  [A] Markdown saved: {out_path.name}  ({len(md):,} chars)")
    return out_path


# ─── Pass 2 — Markdown tree parser ───────────────────────────────────────────

_SECTION_NUM_RE = re.compile(r"^(\d+(?:\.\d+){0,3})\s+(.*)")
_MIN_FEE_CHARS  = 60


@dataclass
class MarkdownSection:
    level:        int
    number:       str
    title:        str
    heading_line: str
    direct_lines: List[str]
    children:     List["MarkdownSection"] = field(default_factory=list)

    @property
    def section_id(self) -> str:
        return self.number or self.title[:40]

    @property
    def direct_content(self) -> str:
        return "\n".join(self.direct_lines).strip()

    @property
    def full_content(self) -> str:
        parts = [self.heading_line] + self.direct_lines
        for child in self.children:
            parts.append(child.full_content)
        return "\n".join(parts)


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


# ─── Pass 2 — utilities ───────────────────────────────────────────────────────

def _tariff_year_from_name(name: str) -> str:
    m = re.search(r"FY[\-_]?(\d{4}[\-_]\d{2,4}|\d{4})", name, re.IGNORECASE)
    return m.group(1) if m else ""


def _doc_id(pdf_path: Path, page_start: int, page_end: Optional[int]) -> str:
    key = f"v2:{pdf_path.name}:{page_start}:{page_end or 'end'}"
    return hashlib.md5(key.encode()).hexdigest()[:12]


# ─── Pass 2 — orchestrator ────────────────────────────────────────────────────

def run_pass2(md_path: Path, store: TariffStore, doc_id: str, tariff_year: str, country_override: str = "", version_tag: Optional[str] = None) -> str:
    full_md  = md_path.read_text(encoding="utf-8")
    all_secs = parse_markdown_tree(full_md)

    annotated = _annotate_section_tree(all_secs)
    units     = _find_sections_to_process(all_secs)

    graph_path = md_path.with_suffix("").with_name(md_path.stem + "-graph.json")
    graph_payload = {
        "md_file": md_path.name, "doc_id": doc_id,
        "total_top_level": len(all_secs), "processing_units": len(units),
        "rules": {"A": "direct fee content → one LLM call", "B": "recurse to children", "C": "skip", "included_in_parent": "bundled"},
        "tree": annotated,
    }
    graph_path.write_text(json.dumps(graph_payload, indent=2, ensure_ascii=False), encoding="utf-8")

    log.info("=" * 60)
    log.info(f"Pass 2 section detection — {md_path.name}")
    log.info(f"  [A]=send  [B]=recurse  [C]=skip")
    log.info("-" * 60)
    _log_annotated_tree(annotated)
    log.info("-" * 60)
    log.info(f"  {len(units)} section(s) selected for LLM processing")
    log.info("=" * 60)

    if version_tag is None:
        version_tag = tariff_year or hashlib.md5(f"{md_path.name}:{doc_id}".encode()).hexdigest()[:8]

    log.info(f"Pass 2 start: {md_path.name}  {len(units)} sections  doc_id={doc_id}")

    skipped = 0
    for sec in tqdm(units, desc=md_path.stem, unit="section"):
        section_id = sec.section_id
        if store.is_section_done(doc_id, section_id):
            skipped += 1
            continue
        section_md = sec.full_content
        if len(section_md.strip()) < _MIN_FEE_CHARS:
            continue
        try:
            payload  = _extract_section_fees(section_md, sec.number, sec.title)
            country  = country_override or payload.country.strip()
            currency = payload.currency.strip()
            for item in payload.fees:
                if not item.section_name:
                    item.section_name = sec.title
                store.upsert_fee_item(doc_id, country, currency, tariff_year, item)
            store.log_section(doc_id, section_id, "done")
            log.info(f"  {sec.number or '—'} {sec.title[:40]}: {len(payload.fees)} fee item(s)")
        except Exception as exc:
            store.log_section(doc_id, section_id, f"error: {exc}")
            log.error(f"  Section '{section_id}' failed: {exc}")
        time.sleep(_INTER_BATCH_DELAY)

    status = store.get_ingestion_status(doc_id)
    save_active_version(country_override or "Unknown", version_tag)
    log.info(
        f"Pass 2 complete — version={version_tag}  {status['total_fee_items']} fee items stored"
        + (f", {skipped} sections skipped" if skipped else "")
        + (f", {status['errors']} section(s) with errors" if status["errors"] else "")
    )
    return version_tag


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")

    parser = argparse.ArgumentParser(description="Document Preparation Agent — Two-Pass PDF Extraction")
    parser.add_argument("--pass", dest="run_pass", choices=["1", "2", "all"], default="all")
    parser.add_argument("--country", default="South Africa", help="Country name, e.g. 'South Africa'")
    parser.add_argument("--version-tag", default=None, help="Version label, e.g. 'FY2025-26-v3.2'")
    parser.add_argument("--page-start", type=int, default=1)
    parser.add_argument("--page-end", type=int, default=None)
    parser.add_argument("--two-column-layout", action="store_true", default=False)
    parser.add_argument("--md-path", type=Path, default=None)
    args = parser.parse_args()

    pdf_files = sorted(RAW_DIR.glob("*.pdf"))
    if not pdf_files:
        log.error(f"No PDF files found in {RAW_DIR}")
        raise SystemExit(1)
    log.info(f"Found {len(pdf_files)} PDF(s) in {RAW_DIR}")

    for pdf_path in pdf_files:
        tariff_year = _tariff_year_from_name(pdf_path.name)
        version_tag = args.version_tag or tariff_year or pdf_path.stem

        if args.run_pass in ("1", "all"):
            md_path = run_pass1(pdf_path, page_start=args.page_start, page_end=args.page_end,
                                two_column_layout=args.two_column_layout,
                                country=args.country, version_tag=version_tag)
        else:
            md_path = args.md_path
            if not md_path:
                log.error("--pass 2 requires --md-path")
                raise SystemExit(1)

        if args.run_pass in ("2", "all"):
            target_db = db_path_for_version(args.country, version_tag)
            store     = TariffStore(target_db)
            did       = _doc_id(pdf_path, args.page_start, args.page_end)
            try:
                used_tag = run_pass2(md_path, store, doc_id=did, tariff_year=tariff_year,
                                     country_override=args.country, version_tag=version_tag)
                log.info(f"Active version set to: {used_tag} for {args.country}")
            finally:
                store.close()
