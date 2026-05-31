"""
Hierarchical Chunk Agent  —  Stage 1 §2.2.2
============================================
Produces the hierarchical chunk index as an INDEPENDENT data source.

This agent reads the same raw PDF as document_preparation_agent.py but
outputs a structurally different artifact: verbatim text blocks organised
into a parent → child section tree.

Purpose (from §2.2.2):
  Provide raw-text context to the Stage-2 Retriever Agent when the
  structured fee data has ambiguity — e.g. a condition that references
  another section, an exception that depends on vessel type not captured
  in the fee item record, or a note that qualifies multiple fees.

Output store:  context-layer/rag/chunks/hierarchical_store.db
  Table: hierarchical_chunks   — one row per text block
  Table: chunk_fts             — FTS5 virtual table for full-text search
  Table: chunk_ingestion_log   — batch-level completion for resumable pipeline

Query patterns:
  -- All exception chunks for a named fee item
  SELECT * FROM hierarchical_chunks
  WHERE fee_item_name = 'PILOTAGE SERVICES' AND chunk_type = 'exceptions'

  -- Full-text search across all chunks
  SELECT hc.* FROM chunk_fts fts
  JOIN hierarchical_chunks hc ON hc.rowid = fts.rowid
  WHERE chunk_fts MATCH 'outside ordinary hours'

  -- Full section tree (parent + all children)
  SELECT * FROM hierarchical_chunks
  WHERE section = '3.3' ORDER BY parent_chunk_id NULLS FIRST

4-level metadata hierarchy stored on every chunk:
  L0  Document   — tariff book title  e.g. "Tariff Book FY 2025-26"
  L1  Category   — top-level service group  e.g. "Marine Services"
  L2  Fee Item   — numbered fee item = PARENT  e.g. "3.4 PILOTAGE SERVICES"
  L3  Chunk type — structural role = CHILD  e.g. "surcharges"

Chunk types (mirrors §2.2.2 section hierarchy):
  section_intro | rule | table | conditions | exceptions |
  surcharges | formula | notes | definition | references | unclassified
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
from typing import Dict, List, Optional, Tuple

import google.generativeai as genai
import pdfplumber
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from tqdm import tqdm

load_dotenv()

# ─── Configuration ────────────────────────────────────────────────────────────

BASE_DIR      = Path(__file__).resolve().parent.parent
RAW_DIR       = BASE_DIR / "context-layer" / "rag" / "raw"
CHUNK_DIR     = BASE_DIR / "context-layer" / "rag" / "chunks"
CHUNK_DB_PATH = CHUNK_DIR / "hierarchical_store.db"

# Tariff store path — read-only, used to resolve section → fee_item_name
TARIFF_DB_PATH = BASE_DIR / "context-layer" / "structured" / "tariff_store.db"

BATCH_SIZE = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

CHUNK_DIR.mkdir(parents=True, exist_ok=True)

# ─── Gemini SDK Setup ─────────────────────────────────────────────────────────

genai.configure(api_key=os.environ["GOOGLE_API_KEY"])

_SYSTEM_PROMPT = """\
You are a document structure analyst specialising in maritime port tariff documents.
Your task is to decompose each document page into its logical text blocks and return
each block with a 4-level metadata hierarchy.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DOCUMENT HIERARCHY  (mandatory on every chunk)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  L0  Document  — the tariff book itself
                  e.g. "Tariff Book FY 2025-26"
                  Identical for every chunk in the same document.

  L1  Category  — the top-level service grouping the fee belongs to
                  e.g. "Marine Services", "Port Services", "General Conditions",
                       "Light Dues", "Vessel Traffic Services"
                  Derive this from the chapter or part heading above the section.

  L2  Fee Item  — THE PARENT LEVEL. A specific, numbered, named tariff fee item.
                  Format: "<section_number> <FEE ITEM TITLE IN UPPERCASE>"
                  e.g. "3.4 PILOTAGE SERVICES"
                       "3.6 TUGS / VESSEL ASSISTANCE"
                  Every chunk must be anchored to exactly one L2 parent.
                  If a block introduces or opens that fee item, its chunk_type is
                  section_intro and it IS the L2 parent record.

  L3  Chunk type — THE CHILD LEVEL. The structural role of this text block
                   inside its L2 fee item parent.
                   Pick exactly one from this list:
                     section_intro — opening paragraph / title of the fee item (= L2 parent node)
                     rule          — a binding rule or regulatory requirement
                     table         — a rate or fee table (all rows verbatim)
                     conditions    — applicability conditions / prerequisites
                     exceptions    — exempt categories or exclusion clauses
                     surcharges    — additional charges triggered by timing or circumstance
                     formula       — a mathematical formula or calculation method
                     notes         — advisory or explanatory notes
                     definition    — defined terms and their meanings
                     references    — cross-references to other sections or documents
                     unclassified  — text that does not fit the above types

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
EXTRACTION RULES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Content fidelity  — copy every text block verbatim; never summarise or paraphrase.
2. One chunk per role — each logical block gets exactly one chunk_type (L3); if a
                        paragraph covers two roles, split it into two chunks.
3. L2 anchoring      — every chunk must have a non-empty meta_L2 referencing its fee
                        item parent. If a block belongs to general conditions before
                        any specific fee item, set meta_L2 = "GENERAL CONDITIONS".
4. Section numbers   — extract the section number exactly as printed (e.g. "3.6.1").
5. Fee item name     — identify the fee item in UPPERCASE from the section heading.
6. Cross-references  — list any section numbers explicitly mentioned inside the block.
7. Page numbers      — 1-indexed page where the block appears.
8. Completeness      — every meaningful line of text must appear in exactly one chunk.
"""

_model = genai.GenerativeModel(
    model_name="gemini-2.5-flash",
    system_instruction=_SYSTEM_PROMPT,
)

_generation_config = genai.GenerationConfig(
    response_mime_type="application/json",
    temperature=0.0,
)

# ─── Pydantic Output Schema ───────────────────────────────────────────────────

VALID_CHUNK_TYPES = {
    "section_intro", "rule", "table", "conditions", "exceptions",
    "surcharges", "formula", "notes", "definition", "references", "unclassified",
}


class RawChunk(BaseModel):
    # ── content ───────────────────────────────────────────────────────────────
    section:          str       = Field(description="Section number exactly as printed e.g. '3.6'")
    fee_item_name:    str       = Field(default="", description="Fee item title in UPPERCASE e.g. 'PILOTAGE SERVICES'")
    chunk_type:       str       = Field(description="L3 — structural role, one of the 11 chunk types")
    content:          str       = Field(description="Verbatim text of this block, never summarised")
    parent_section:   str       = Field(default="", description="Immediate parent section number")
    cross_references: List[str] = Field(default_factory=list, description="Section numbers explicitly mentioned inside content")
    source_page:      int       = Field(default=0, description="1-indexed page number")
    # ── 4-level hierarchy metadata ────────────────────────────────────────────
    meta_L0: str = Field(
        default="",
        description="L0 — Document title e.g. 'Tariff Book FY 2025-26'",
    )
    meta_L1: str = Field(
        default="",
        description="L1 — Top-level service category e.g. 'Marine Services', 'Port Services'",
    )
    meta_L2: str = Field(
        default="",
        description="L2 — Fee item parent: '<section> <FEE ITEM NAME>' e.g. '3.4 PILOTAGE SERVICES'",
    )


class RawChunkPayload(BaseModel):
    port:   str            = Field(description="Port name from document context")
    chunks: List[RawChunk] = Field(default_factory=list)


# ─── Section → Fee Item Lookup ────────────────────────────────────────────────

def _load_section_map(doc_id: str) -> Dict[str, str]:
    """
    Read the tariff_store (if available) to build section → fee_item_name mapping.
    This lets the chunk agent link chunks to named fee items even when the LLM
    returns an empty fee_item_name for a sub-section block.
    """
    if not TARIFF_DB_PATH.exists():
        return {}
    try:
        conn = sqlite3.connect(str(TARIFF_DB_PATH))
        rows = conn.execute(
            "SELECT section, tariff_fee_item FROM tariff_fee_items WHERE doc_id=?",
            (doc_id,),
        ).fetchall()
        conn.close()
        return {row[0]: row[1] for row in rows}
    except Exception:
        return {}


# ─── SQLite Persistence ───────────────────────────────────────────────────────

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS hierarchical_chunks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id          TEXT    NOT NULL,
    chunk_id        TEXT    NOT NULL,
    parent_chunk_id TEXT,
    section         TEXT    NOT NULL,
    fee_item_name   TEXT    DEFAULT '',
    port            TEXT    DEFAULT '',
    chunk_type      TEXT    NOT NULL,
    content         TEXT    NOT NULL,
    related_chunks  TEXT    DEFAULT '[]',
    source_page     INTEGER DEFAULT 0,
    meta_L0         TEXT    DEFAULT '',
    meta_L1         TEXT    DEFAULT '',
    meta_L2         TEXT    DEFAULT '',
    ingested_at     TEXT    NOT NULL,
    UNIQUE(doc_id, chunk_id)
);

CREATE INDEX IF NOT EXISTS idx_hc_section       ON hierarchical_chunks(section);
CREATE INDEX IF NOT EXISTS idx_hc_fee_item      ON hierarchical_chunks(fee_item_name);
CREATE INDEX IF NOT EXISTS idx_hc_type          ON hierarchical_chunks(chunk_type);
CREATE INDEX IF NOT EXISTS idx_hc_fee_type      ON hierarchical_chunks(fee_item_name, chunk_type);
CREATE INDEX IF NOT EXISTS idx_hc_parent        ON hierarchical_chunks(parent_chunk_id);
CREATE INDEX IF NOT EXISTS idx_hc_L1            ON hierarchical_chunks(meta_L1);
CREATE INDEX IF NOT EXISTS idx_hc_L2            ON hierarchical_chunks(meta_L2);

-- FTS5: tokenised search across hierarchy labels + verbatim content
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
    chunk_id,
    meta_L0,
    meta_L1,
    meta_L2,
    chunk_type,
    content,
    tokenize = 'porter ascii'
);

CREATE TABLE IF NOT EXISTS chunk_ingestion_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id      TEXT    NOT NULL,
    batch_start INTEGER NOT NULL,
    batch_end   INTEGER NOT NULL,
    status      TEXT    NOT NULL,
    logged_at   TEXT    NOT NULL,
    UNIQUE(doc_id, batch_start, batch_end)
);
"""


class ChunkStore:
    """
    Persistence layer for §2.2.2 Hierarchical Chunks.

    Separate from TariffStore — this is an independent data source.
    FTS5 virtual table (chunk_fts) enables full-text search across
    all verbatim content blocks.
    """

    def __init__(self, db_path: Path) -> None:
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA_SQL)
        self.conn.commit()
        log.info(f"ChunkStore ready: {db_path}")

    # ── write ──────────────────────────────────────────────────────────────

    def upsert_chunk(
        self,
        doc_id: str,
        chunk_id: str,
        parent_chunk_id: Optional[str],
        section: str,
        fee_item_name: str,
        port: str,
        chunk_type: str,
        content: str,
        related_chunks: List[str],
        source_page: int,
        meta_L0: str = "",
        meta_L1: str = "",
        meta_L2: str = "",
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            INSERT INTO hierarchical_chunks
                (doc_id, chunk_id, parent_chunk_id, section, fee_item_name, port,
                 chunk_type, content, related_chunks, source_page,
                 meta_L0, meta_L1, meta_L2, ingested_at)
            VALUES (?,?,?,?,?,?, ?,?,?,?, ?,?,?,?)
            ON CONFLICT(doc_id, chunk_id) DO UPDATE SET
                fee_item_name   = excluded.fee_item_name,
                content         = excluded.content,
                related_chunks  = excluded.related_chunks,
                meta_L0         = excluded.meta_L0,
                meta_L1         = excluded.meta_L1,
                meta_L2         = excluded.meta_L2,
                ingested_at     = excluded.ingested_at
            """,
            (
                doc_id, chunk_id, parent_chunk_id, section, fee_item_name, port,
                chunk_type, content, json.dumps(related_chunks), source_page,
                meta_L0, meta_L1, meta_L2, now,
            ),
        )
        # Keep FTS5 in sync — delete old entry then insert fresh
        self.conn.execute("DELETE FROM chunk_fts WHERE chunk_id = ?", (chunk_id,))
        self.conn.execute(
            """
            INSERT INTO chunk_fts (chunk_id, meta_L0, meta_L1, meta_L2, chunk_type, content)
            VALUES (?,?,?,?,?,?)
            """,
            (chunk_id, meta_L0, meta_L1, meta_L2, chunk_type, content),
        )
        self.conn.commit()

    def log_batch(self, doc_id: str, start: int, end: int, status: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            INSERT INTO chunk_ingestion_log (doc_id, batch_start, batch_end, status, logged_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(doc_id, batch_start, batch_end)
            DO UPDATE SET status = excluded.status, logged_at = excluded.logged_at
            """,
            (doc_id, start, end, status, now),
        )
        self.conn.commit()

    def is_batch_done(self, doc_id: str, start: int, end: int) -> bool:
        row = self.conn.execute(
            "SELECT status FROM chunk_ingestion_log WHERE doc_id=? AND batch_start=? AND batch_end=?",
            (doc_id, start, end),
        ).fetchone()
        return row is not None and row["status"] == "done"

    # ── read ───────────────────────────────────────────────────────────────

    def query_by_fee_and_type(
        self, fee_item_name: str, chunk_type: Optional[str] = None
    ) -> List[dict]:
        """Retrieve all chunks for a fee item, optionally filtered by type."""
        if chunk_type:
            rows = self.conn.execute(
                """
                SELECT * FROM hierarchical_chunks
                WHERE fee_item_name = ? AND chunk_type = ?
                ORDER BY source_page, id
                """,
                (fee_item_name, chunk_type),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT * FROM hierarchical_chunks
                WHERE fee_item_name = ?
                ORDER BY chunk_type, source_page
                """,
                (fee_item_name,),
            ).fetchall()
        return [dict(r) for r in rows]

    def query_by_section(self, section: str) -> List[dict]:
        """Return the full section tree: parent chunk + all children."""
        rows = self.conn.execute(
            """
            SELECT * FROM hierarchical_chunks
            WHERE section = ? OR section LIKE ?
            ORDER BY parent_chunk_id NULLS FIRST, id
            """,
            (section, f"{section}.%"),
        ).fetchall()
        return [dict(r) for r in rows]

    def full_text_search(self, query: str, limit: int = 20) -> List[dict]:
        """
        FTS5 Porter-stemmed search across L0/L1/L2 labels + verbatim content.
        Results are joined back to hierarchical_chunks via chunk_id for full metadata.
        """
        rows = self.conn.execute(
            """
            SELECT hc.*, fts.rank
            FROM chunk_fts fts
            JOIN hierarchical_chunks hc ON hc.chunk_id = fts.chunk_id
            WHERE chunk_fts MATCH ?
            ORDER BY fts.rank
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_ingestion_status(self, doc_id: str) -> dict:
        rows = self.conn.execute(
            "SELECT * FROM chunk_ingestion_log WHERE doc_id=? ORDER BY batch_start",
            (doc_id,),
        ).fetchall()
        total = self.conn.execute(
            "SELECT COUNT(*) FROM hierarchical_chunks WHERE doc_id=?", (doc_id,)
        ).fetchone()[0]
        batches = [dict(r) for r in rows]
        return {
            "doc_id": doc_id,
            "total_batches": len(batches),
            "done": sum(1 for b in batches if b["status"] == "done"),
            "errors": sum(1 for b in batches if b["status"].startswith("error")),
            "total_chunks": total,
            "batches": batches,
        }

    def close(self) -> None:
        self.conn.close()


# ─── Chunk ID Generation ──────────────────────────────────────────────────────

def _make_chunk_id(section: str, chunk_type: str, content: str) -> str:
    """
    Deterministic chunk_id: section + type + short content hash.
    Stable across re-runs so upsert correctly deduplicates.
    """
    content_hash = hashlib.md5(content[:120].encode()).hexdigest()[:8]
    safe_section = re.sub(r"[^\w.]", "_", section)
    return f"{safe_section}.{chunk_type}.{content_hash}"


def _parent_chunk_id_for(
    section: str, chunk_type: str, section_intros: Dict[str, str]
) -> Optional[str]:
    """
    Return the chunk_id of the parent section_intro chunk, if it exists.
    Children (rule, table, conditions, etc.) attach to their section's intro.
    """
    if chunk_type == "section_intro":
        return None
    return section_intros.get(section)


# ─── LLM Call with Retry ─────────────────────────────────────────────────────

def _extract_chunks(context: str, max_retries: int = 3) -> RawChunkPayload:
    schema = RawChunkPayload.model_json_schema()
    prompt = (
        "Decompose the document pages below into hierarchical text chunks. "
        "Return JSON matching this schema exactly:\n\n"
        f"{json.dumps(schema, indent=2)}\n\n"
        "Pages:\n\n"
        f"{context}"
    )
    for attempt in range(max_retries):
        try:
            response = _model.generate_content(prompt, generation_config=_generation_config)
            return RawChunkPayload.model_validate_json(response.text)
        except Exception as exc:
            if attempt == max_retries - 1:
                raise
            wait = 2 ** attempt
            log.warning(f"LLM call failed (attempt {attempt + 1}): {exc} — retrying in {wait}s")
            time.sleep(wait)


# ─── PDF Loading (shared with document_preparation_agent) ────────────────────

def load_pdf_pages(pdf_path: Path) -> List[str]:
    pages: List[str] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            plain  = page.extract_text(x_tolerance=3, y_tolerance=3) or ""
            tables = page.extract_tables()
            if tables:
                table_blocks = []
                for tbl in tables:
                    rows = [
                        "\t".join((cell or "").strip() for cell in row)
                        for row in tbl if any(row)
                    ]
                    table_blocks.append("\n".join(rows))
                pages.append(plain + "\n\n[TABLE]\n" + "\n\n[TABLE]\n".join(table_blocks))
            else:
                pages.append(plain)
    return pages


# ─── Helpers ──────────────────────────────────────────────────────────────────

def doc_id_from_path(pdf_path: Path) -> str:
    return hashlib.md5(pdf_path.name.encode()).hexdigest()[:12]


# ─── Pipeline ─────────────────────────────────────────────────────────────────

def run_hierarchical_chunking(pdf_path: Path, store: ChunkStore) -> None:
    """
    Full §2.2.2 pipeline for one PDF:
      1. Load section → fee_item_name map from tariff_store (if available).
      2. Load all pages with layout-aware extraction.
      3. Iterate page batches — skip already-completed batches (resumable).
      4. Call Gemini to classify text blocks into typed chunks.
      5. Generate deterministic chunk_ids and resolve parent linkage.
      6. Enrich fee_item_name from section map where the LLM left it blank.
      7. Persist to hierarchical_chunks + FTS5 index.

    Can run independently of document_preparation_agent.py.
    If the tariff_store exists, fee_item_name linkage is enriched automatically.
    """
    doc_id      = doc_id_from_path(pdf_path)
    section_map = _load_section_map(doc_id)
    if section_map:
        log.info(f"Loaded {len(section_map)} section→fee mappings from tariff_store")
    else:
        log.info("tariff_store not available — fee_item_name will rely on LLM extraction only")

    log.info(f"Chunk pipeline start: {pdf_path.name}  doc_id={doc_id}")
    pages = load_pdf_pages(pdf_path)
    total = len(pages)
    log.info(f"Loaded {total} pages")

    skipped = 0
    for batch_start in tqdm(range(0, total, BATCH_SIZE), desc=f"{pdf_path.stem} [chunks]", unit="batch"):
        batch_end = min(batch_start + BATCH_SIZE, total)

        if store.is_batch_done(doc_id, batch_start, batch_end):
            skipped += 1
            continue

        context = "\n\n".join(
            f"=== PAGE {i + 1} ===\n{pages[i]}"
            for i in range(batch_start, batch_end)
        )

        try:
            payload = _extract_chunks(context)
            port    = payload.port.strip() or "Unknown"

            # Track section_intro chunk_ids so children can reference their parent
            section_intros: Dict[str, str] = {}

            for raw in payload.chunks:
                # Normalise chunk_type (L3)
                ctype = raw.chunk_type.lower().strip()
                if ctype not in VALID_CHUNK_TYPES:
                    ctype = "unclassified"

                # Resolve fee_item_name: LLM value → section map fallback
                fee_name = raw.fee_item_name.strip()
                if not fee_name and raw.section in section_map:
                    fee_name = section_map[raw.section]
                if not fee_name and raw.parent_section in section_map:
                    fee_name = section_map[raw.parent_section]

                # Build L2 label: prefer LLM-provided meta_L2, fall back to
                # constructing it from section + fee_name
                meta_L2 = raw.meta_L2.strip()
                if not meta_L2 and raw.section and fee_name:
                    meta_L2 = f"{raw.section} {fee_name}"
                elif not meta_L2 and fee_name:
                    meta_L2 = fee_name

                chunk_id = _make_chunk_id(raw.section, ctype, raw.content)

                # section_intro chunk = the L2 parent node; register so children can link
                if ctype == "section_intro":
                    section_intros[raw.section] = chunk_id

                parent_id = _parent_chunk_id_for(raw.section, ctype, section_intros)

                store.upsert_chunk(
                    doc_id=doc_id,
                    chunk_id=chunk_id,
                    parent_chunk_id=parent_id,
                    section=raw.section,
                    fee_item_name=fee_name,
                    port=port,
                    chunk_type=ctype,
                    content=raw.content,
                    related_chunks=raw.cross_references,
                    source_page=raw.source_page,
                    meta_L0=raw.meta_L0.strip(),
                    meta_L1=raw.meta_L1.strip(),
                    meta_L2=meta_L2,
                )

            store.log_batch(doc_id, batch_start, batch_end, "done")
            log.info(
                f"  Pages {batch_start + 1}–{batch_end}: "
                f"{len(payload.chunks)} chunks  (port={port})"
            )
        except Exception as exc:
            store.log_batch(doc_id, batch_start, batch_end, f"error: {exc}")
            log.error(f"  Pages {batch_start + 1}–{batch_end} failed: {exc}")

    status = store.get_ingestion_status(doc_id)
    log.info(
        f"Chunk pipeline complete — {status['total_chunks']} chunks stored"
        + (f", {skipped} batches skipped" if skipped else "")
        + (f", {status['errors']} batch(es) with errors" if status["errors"] else "")
    )


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    store = ChunkStore(CHUNK_DB_PATH)
    try:
        pdf_files = sorted(RAW_DIR.glob("*.pdf"))
        if not pdf_files:
            log.error(f"No PDF files found in {RAW_DIR}")
        else:
            log.info(f"Found {len(pdf_files)} PDF(s) in {RAW_DIR}")
            for pdf_path in pdf_files:
                run_hierarchical_chunking(pdf_path, store)
    finally:
        store.close()
