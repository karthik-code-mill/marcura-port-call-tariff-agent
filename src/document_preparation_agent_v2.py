"""
Document Preparation Agent v2 — Two-Pass PDF Extraction Pipeline
================================================================
Pass 1: PDF → Markdown + Section Graph
    Scans the document for hierarchical section structure (L0–L4) using
    section-number pattern matching and font-size analysis (PyMuPDF / fitz),
    then converts the page range to LLM-ready Markdown via pymupdf4llm,
    preserving:
        • Section / sub-section headings at the correct heading level
        • Tables as GitHub-flavoured Markdown pipe tables
        • Two-column / booklet scan layout (optional)
    Artefacts written to  context-layer/prepared/:
        <stem>_v2.md      — full Markdown document
        <stem>_graph.json — hierarchical section graph (L0–L4)

Pass 2: Markdown + Graph → Structured Fee Data (SQLite)
    Reads the artefacts from Pass 1, traverses every leaf section in the
    graph and sends each section's Markdown snippet to Gemini for structured
    fee extraction.  The LLM works purely from section content — no external
    metadata is injected into the prompt.  Extracted values are stored as
    indexed SQLite columns for query-time filtering:
        port         — All / named port / Other  (LLM-extracted per fee)
        vessel_type  — All / specific type       (LLM-extracted per fee)
        country      — document country; --country CLI arg overrides LLM value
    Output schema adds two new columns over v1:
        section_name TEXT — human-readable heading of the containing section.
        vessel_type  TEXT — vessel/cargo type applicability ('All' when unrestricted).

CLI:
    python document_preparation_agent_v2.py --pass 1 [options]
    python document_preparation_agent_v2.py --pass 2 [options]
    python document_preparation_agent_v2.py --pass all [options]
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import hashlib
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import fitz                    # PyMuPDF
import pymupdf4llm
from google import genai
from google.genai import types
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from tqdm import tqdm

load_dotenv()

# ─── Configuration ────────────────────────────────────────────────────────────

BASE_DIR     = Path(__file__).resolve().parent.parent
RAW_DIR      = BASE_DIR / "context-layer" / "rag" / "raw"
PREPARED_DIR = BASE_DIR / "context-layer" / "prepared"   # Pass 1 artefacts
STORE_DIR    = BASE_DIR / "context-layer" / "structured"
DB_PATH      = STORE_DIR / "tariff_store_v2.db"
PIPELINE_CONFIG_PATH = STORE_DIR / "pipeline_config.json"

PREPARED_DIR.mkdir(parents=True, exist_ok=True)
STORE_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def db_path_for_version(version_tag: str) -> Path:
    safe = re.sub(r"[^\w\-.]", "_", version_tag)
    return STORE_DIR / f"tariff_store_v2_{safe}.db"


def save_active_version(version_tag: str) -> None:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    config = _load_raw_config()
    if version_tag not in config.get("versions_v2", []):
        config.setdefault("versions_v2", []).append(version_tag)
    config["active_version_v2"] = version_tag
    PIPELINE_CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")


def load_active_version() -> str | None:
    return _load_raw_config().get("active_version_v2")


def _load_raw_config() -> dict:
    if PIPELINE_CONFIG_PATH.exists():
        try:
            return json.loads(PIPELINE_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


# ─── Section Graph ─────────────────────────────────────────────────────────────

@dataclass
class SectionNode:
    """
    One node in the document section graph.

    level   — 0 = document root, 1 = L1 heading, …, 4 = L4 heading.
    number  — section number exactly as printed, e.g. "3.1.2".  Empty for
              unnumbered headings detected via font-size only.
    title   — heading text without the section number prefix.
    id      — stable unique key: number if present, otherwise auto-generated.
    """
    id:         str
    level:      int
    title:      str
    number:     str
    page_start: int
    page_end:   int
    children:   List["SectionNode"] = field(default_factory=list)

    # ── serialisation helpers ──────────────────────────────────────────────────
    def to_dict(self) -> dict:
        d = asdict(self)
        d["children"] = [c.to_dict() for c in self.children]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "SectionNode":
        children = [cls.from_dict(c) for c in d.pop("children", [])]
        node = cls(**d)
        node.children = children
        return node

    def is_leaf(self) -> bool:
        return len(self.children) == 0

    def all_leaves(self) -> List["SectionNode"]:
        """Return all leaf descendants (depth-first)."""
        if self.is_leaf():
            return [self]
        leaves: List[SectionNode] = []
        for child in self.children:
            leaves.extend(child.all_leaves())
        return leaves

    def all_nodes(self) -> List["SectionNode"]:
        """Return self + all descendants (depth-first)."""
        result = [self]
        for child in self.children:
            result.extend(child.all_nodes())
        return result


def _level_from_number(number: str) -> int:
    """'3.1.2' → level 3;  '3' → level 1;  '' → 0."""
    return len(number.split(".")) if number else 0


# ─── Section-number regex (shared) ───────────────────────────────────────────
# Matches "3", "3.1", "3.1.2", "3.1.2.1" at the start of a line.
_SECTION_NUM_RE = re.compile(r"^(\d+(?:\.\d+){0,3})\s+(.*)")


# ─── [DISABLED — FUTURE ENHANCEMENT] Pass 1 Structure Scanner ────────────────
# Graph-based section detection was disabled because it was unreliable on
# scanned / booklet PDFs with table numbers causing false positives.
# Re-enable when:
#   • PDF source consistently provides embedded bookmarks (fitz.get_toc), OR
#   • LLM token limits allow feeding the full document for TOC extraction.
# All functions below (_synthesize_missing_parents … scan_document_structure)
# are kept for reference and can be reinstated without API changes.


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _synthesize_missing_parents(entries: List[dict]) -> List[dict]:
    """
    Insert synthetic parent nodes for any missing ancestor section numbers.

    Example: "3.1" present but "3" absent → synthetic "3" is inserted so the
    tree is always fully connected.
    """
    seen   = {e["number"] for e in entries if e["number"]}
    extras: List[dict] = []

    for e in entries:
        if not e["number"]:
            continue
        parts = e["number"].split(".")
        for depth in range(1, len(parts)):
            ancestor = ".".join(parts[:depth])
            if ancestor not in seen:
                seen.add(ancestor)
                extras.append({
                    "id":         ancestor,
                    "level":      depth,
                    "number":     ancestor,
                    "title":      f"Section {ancestor}",
                    "page_start": e["page_start"],
                    "page_end":   e["page_start"],
                })

    combined = entries + extras

    def _key(e: dict):
        if e["number"]:
            return (0, tuple(int(p) for p in e["number"].split(".")))
        return (1, ())

    combined.sort(key=_key)
    return combined


def _build_tree_from_entries(
    entries: List[dict],
    page_start: int,
    doc_last_page: int,
) -> SectionNode:
    """Build a SectionNode tree from a flat ordered list of heading dicts."""
    root  = SectionNode(
        id="root", level=0, title="Document", number="",
        page_start=page_start, page_end=doc_last_page,
    )
    stack: List[SectionNode] = [root]

    for e in entries:
        node = SectionNode(
            id=e.get("number") or e["id"],
            level=e["level"],
            title=e["title"],
            number=e.get("number", ""),
            page_start=e.get("page_start", 0),
            page_end=e.get("page_start", 0),
        )
        while len(stack) > 1 and stack[-1].level >= node.level:
            stack.pop()
        stack[-1].children.append(node)
        stack.append(node)

    _propagate_page_ends(root, doc_last_page)
    return root


# ── Strategy 1 — PDF embedded bookmarks ───────────────────────────────────────

def _toc_from_pdf_bookmarks(
    pdf_path: Path,
    page_start: int,
    doc_last_page: int,
) -> Optional[SectionNode]:
    """
    Build the section graph from the PDF's embedded bookmark outline via
    fitz.get_toc().  Returns None if the PDF has no outline.

    This is the fastest and most accurate method — zero LLM calls, correct
    hierarchy and page numbers directly from the PDF metadata.
    """
    doc = fitz.open(str(pdf_path))
    toc = doc.get_toc()   # [[level, title, page], …]
    doc.close()

    if not toc:
        log.info("  No PDF bookmark outline found.")
        return None

    entries: List[dict] = []
    auto_id = 0
    for level, title, page in toc:
        title = title.strip()
        nm    = _SECTION_NUM_RE.match(title)
        if nm:
            number = nm.group(1)
            clean  = nm.group(2).strip()
        else:
            number = ""
            clean  = title
            auto_id += 1
        entries.append({
            "id":         number or f"_bm{auto_id}",
            "level":      level,
            "number":     number,
            "title":      clean,
            "page_start": page,
            "page_end":   page,
        })

    entries = _synthesize_missing_parents(entries)
    root    = _build_tree_from_entries(entries, page_start, doc_last_page)
    log.info(f"  Structure from PDF bookmarks: {len(toc)} entries")
    return root


# ── Strategy 2 — LLM TOC extraction ───────────────────────────────────────────

_TOC_EXTRACT_PROMPT = """\
Find and extract the complete Table of Contents from the document below.

Return a JSON array. Each element must have exactly these keys:
  "number" — section number string exactly as printed (e.g. "3.1.2"); \
use "" for unnumbered entries
  "title"  — section title WITHOUT the section-number prefix
  "level"  — nesting depth as integer (1=top level, 2=sub-section, \
3=sub-sub-section …)
  "page"   — the page number as printed in the document (integer; 0 if absent)

Rules:
  • Only extract actual TOC entries. Ignore body text, running headers, footers.
  • If a TOC page is clearly present, extract ALL entries including sub-sections.
  • If no TOC page is found at all, return [].
  • Never invent entries not visible in the source.

Return ONLY the JSON array — no markdown fences, no explanation.
"""


def _toc_from_llm(
    pdf_path: Path,
    page_start: int,
    doc_last_page: int,
    two_column_layout: bool,
    toc_page_end: int,
) -> Optional[SectionNode]:
    """
    Extract TOC structure by sending the first ``toc_page_end`` physical pages
    to Gemini and asking it to return the TOC as a JSON array.

    Returns None if the LLM returns an empty list or all attempts fail.
    """
    toc_md = convert_pdf_to_markdown(
        pdf_path, page_start, toc_page_end, two_column_layout
    )
    prompt  = f"{_TOC_EXTRACT_PROMPT}\n\n---\n\n{toc_md}"
    cfg     = types.GenerateContentConfig(
        response_mime_type="application/json",
        temperature=0.0,
        max_output_tokens=8192,
    )

    available = [m for m in _MODELS if m not in _exhausted_models]
    for model in available:
        try:
            response = _client.models.generate_content(
                model=model, contents=prompt, config=cfg
            )
            raw = json.loads(response.text)
            if not isinstance(raw, list) or not raw:
                log.info("  LLM TOC extraction returned empty list.")
                return None

            entries: List[dict] = []
            auto_id = 0
            for e in raw:
                number = str(e.get("number", "")).strip()
                title  = str(e.get("title",  "")).strip()
                level  = max(1, int(e.get("level", 1)))
                page   = int(e.get("page", 0))
                auto_id += 1
                entries.append({
                    "id":         number or f"_toc{auto_id}",
                    "level":      level,
                    "number":     number,
                    "title":      title,
                    "page_start": page,
                    "page_end":   page,
                })

            entries = _synthesize_missing_parents(entries)
            root    = _build_tree_from_entries(entries, page_start, doc_last_page)
            log.info(f"  Structure from LLM TOC: {len(raw)} entries")
            return root

        except Exception as exc:
            log.warning(f"  LLM TOC extraction failed ({model}): {exc}")

    return None


# ── Strategy 3 — Markdown ATX heading scan (fallback) ─────────────────────────

def _toc_from_markdown_headings(
    markdown: str,
    pdf_path: Path,
    page_start: int,
    page_end: Optional[int],
    doc_last_page: int,
) -> SectionNode:
    """
    Last-resort fallback: extract structure from ATX headings in the Markdown.

    pymupdf4llm emits real document headings as ``#`` lines and never emits
    table numbers as headings, so there are no false positives from table
    content.  Page numbers are back-filled via a lightweight fitz span scan
    that only looks for spans matching already-known section numbers.
    """
    headings: List[dict] = []
    auto_id  = 0
    seen_numbers: set = set()

    for line in markdown.split("\n"):
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if not m:
            continue
        md_level  = len(m.group(1))
        raw_title = m.group(2).strip()

        nm = _SECTION_NUM_RE.match(raw_title)
        if nm:
            number = nm.group(1)
            title  = nm.group(2).strip()
            level  = _level_from_number(number)
            if not title or not title[0].isalpha():
                continue
            if level > 4 or number in seen_numbers:
                continue
            seen_numbers.add(number)
            headings.append({
                "id": number, "level": level, "number": number,
                "title": title, "page_start": 0, "page_end": 0,
            })
        else:
            level = min(md_level, 4)
            auto_id += 1
            headings.append({
                "id": f"_h{auto_id}", "level": level, "number": "",
                "title": raw_title, "page_start": 0, "page_end": 0,
            })

    # Back-fill page numbers via targeted fitz span scan
    num_to_h = {h["number"]: h for h in headings if h["number"]}
    if num_to_h:
        doc     = fitz.open(str(pdf_path))
        end_0   = min((page_end or len(doc)), len(doc)) - 1
        start_0 = page_start - 1
        assigned: set = set()
        for page_idx in range(start_0, end_0 + 1):
            page_num = page_idx + 1
            blocks   = doc[page_idx].get_text(
                "dict", flags=fitz.TEXT_PRESERVE_WHITESPACE
            )["blocks"]
            for block in blocks:
                for line in block.get("lines", []):
                    text = " ".join(
                        s["text"] for s in line.get("spans", [])
                    ).strip()
                    nm2 = _SECTION_NUM_RE.match(text)
                    if nm2:
                        num = nm2.group(1)
                        if num in num_to_h and num not in assigned:
                            num_to_h[num]["page_start"] = page_num
                            assigned.add(num)
        doc.close()

    headings = _synthesize_missing_parents(headings)
    root     = _build_tree_from_entries(headings, page_start, doc_last_page)
    log.info(f"  Structure from Markdown headings: {len(headings)} entries (fallback)")
    return root


# ── Public entry point ─────────────────────────────────────────────────────────

def scan_document_structure(
    pdf_path: Path,
    page_start: int = 1,
    page_end: Optional[int] = None,
    two_column_layout: bool = False,
    precomputed_markdown: Optional[str] = None,
    toc_page_end: Optional[int] = None,
) -> SectionNode:
    """
    Scan the PDF for hierarchical heading structure using a three-strategy
    cascade (fastest / most reliable first):

    1. **PDF bookmarks** (fitz.get_toc) — reads the PDF's embedded outline.
       Zero LLM cost.  Returns instantly with correct hierarchy + page numbers
       if the PDF was authored with an outline.

    2. **LLM TOC extraction** — sends the first ``toc_page_end`` physical
       pages to Gemini and asks it to return the TOC as JSON.  Reliable for
       any document that has a visible Table of Contents page.

    3. **Markdown ATX heading scan** — last resort for unstructured PDFs.
       Reads ``#`` heading lines from the Markdown and back-fills page numbers
       via a targeted fitz scan.

    ``toc_page_end`` — last physical page fed to the LLM for strategy 2
        (default: page_start + 14, i.e. first 15 physical pages).
    ``precomputed_markdown`` — reuse the Markdown from run_pass1 if strategy
        3 is reached, to avoid converting the PDF a third time.
    """
    doc = fitz.open(str(pdf_path))
    doc_last_page = min((page_end or len(doc)), len(doc))
    doc.close()

    log.info(f"  Trying strategy 1: PDF bookmarks …")
    root = _toc_from_pdf_bookmarks(pdf_path, page_start, doc_last_page)
    if root:
        return root

    toc_end = toc_page_end or min(page_start + 14, doc_last_page)
    log.info(f"  Trying strategy 2: LLM TOC extraction (pages {page_start}–{toc_end}) …")
    root = _toc_from_llm(pdf_path, page_start, doc_last_page, two_column_layout, toc_end)
    if root:
        return root

    log.info("  Trying strategy 3: Markdown heading scan …")
    markdown = precomputed_markdown or convert_pdf_to_markdown(
        pdf_path, page_start, page_end, two_column_layout
    )
    return _toc_from_markdown_headings(
        markdown, pdf_path, page_start, page_end, doc_last_page
    )


def _propagate_page_ends(node: SectionNode, doc_last_page: int) -> None:
    """Set page_end for each child based on the next sibling's page_start."""
    for i, child in enumerate(node.children):
        if i + 1 < len(node.children):
            child.page_end = node.children[i + 1].page_start - 1
        else:
            child.page_end = node.page_end
        _propagate_page_ends(child, doc_last_page)


# ─── Pass 1 — Markdown Converter ──────────────────────────────────────────────

def convert_pdf_to_markdown(
    pdf_path: Path,
    page_start: int = 1,
    page_end: Optional[int] = None,
    two_column_layout: bool = False,
) -> str:
    """
    Convert PDF pages to Markdown via pymupdf4llm.

    pymupdf4llm preserves:
      • Heading hierarchy (mapped to # / ## / ### from font sizes)
      • Tables as GitHub-flavoured Markdown pipe tables
      • Bold / italic inline styles

    two_column_layout — when True each physical PDF page is a booklet scan
        containing two side-by-side document pages.  We build a temporary
        in-memory PDF where each physical page is split at its horizontal
        midpoint into a left half-page and a right half-page (in that order),
        so pymupdf4llm reads them in the correct reading order:
            physical page N  →  content page (2N-1) [left]
                             →  content page (2N)   [right]
    """
    doc = fitz.open(str(pdf_path))
    total = len(doc)
    end_idx = min((page_end or total), total)
    pages = list(range(page_start - 1, end_idx))

    if two_column_layout:
        # Build a temporary in-memory PDF with each physical page split into
        # left and right halves as separate pages.
        split_doc = fitz.open()
        for page_idx in pages:
            src   = doc[page_idx]
            r     = src.rect
            mid_x = r.x0 + (r.x1 - r.x0) / 2
            for clip in (
                fitz.Rect(r.x0, r.y0, mid_x, r.y1),   # left half (earlier page)
                fitz.Rect(mid_x, r.y0, r.x1, r.y1),    # right half (later page)
            ):
                new_pg = split_doc.new_page(width=clip.width, height=clip.height)
                new_pg.show_pdf_page(new_pg.rect, doc, page_idx, clip=clip)
        md = pymupdf4llm.to_markdown(split_doc, page_chunks=False)
        split_doc.close()
    else:
        md = pymupdf4llm.to_markdown(doc, pages=pages, page_chunks=False)

    doc.close()
    return md


# ─── Pass 1 — Orchestrator ────────────────────────────────────────────────────

def run_pass1(
    pdf_path: Path,
    page_start: int = 1,
    page_end: Optional[int] = None,
    two_column_layout: bool = False,
    toc_page_end: Optional[int] = None,  # reserved — used by disabled graph builder
    output_dir: Path = PREPARED_DIR,
) -> Path:
    """
    Run Pass 1 for one PDF:
      1. Convert pages to Markdown
      2. Build section graph (bookmarks → LLM TOC → heading scan cascade)
      3. Write <stem>_v2.md and <stem>_graph.json to output_dir

    Returns (md_path, graph_path).
    """
    stem = pdf_path.stem
    md_path    = output_dir / f"{stem}_v2.md"
    graph_path = output_dir / f"{stem}_graph.json"

    log.info(f"Pass 1 start: {pdf_path.name}  pages {page_start}–{page_end or 'end'}")

    # Step A — convert PDF to Markdown
    log.info("  [A] Converting PDF to Markdown...")
    md = convert_pdf_to_markdown(pdf_path, page_start, page_end, two_column_layout)
    md_path.write_text(md, encoding="utf-8")
    log.info(f"  [A] Markdown saved: {md_path.name}  ({len(md):,} chars)")

    # [DISABLED — FUTURE ENHANCEMENT]
    # Graph-based section detection was unreliable on scanned/booklet PDFs.
    # Re-enable when higher LLM token limits make full-document TOC extraction
    # practical, or when the PDF source consistently provides embedded bookmarks.
    #
    # root = scan_document_structure(
    #     pdf_path, page_start, page_end, two_column_layout,
    #     precomputed_markdown=md, toc_page_end=toc_page_end,
    # )
    # graph_path.write_text(
    #     json.dumps(root.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    # )
    # log.info(f"  [B] Section graph saved: {graph_path.name}")

    return md_path


# ─── Pydantic Output Schema (v2 — adds section_name) ─────────────────────────

class SurchargeItem(BaseModel):
    condition:  str   = Field(description="Full trigger condition verbatim from the document.")
    percentage: float = Field(description="Percentage surcharge, e.g. 50.0 means ×1.50.")


class ExceptionItem(BaseModel):
    text:            str          = Field(description="Verbatim exception clause text.")
    machine_handled: bool         = Field(default=False)
    source:          Dict[str, Any] = Field(default_factory=dict)


class TariffFeeItem(BaseModel):
    section:      str = Field(
        description=(
            "Section number exactly as printed, e.g. '3.6', '4.1.2'. "
            "Primary grouping key — copy without modification."
        )
    )
    section_name: str = Field(
        default="",
        description=(
            "Human-readable heading of the section, e.g. 'PILOTAGE SERVICES'. "
            "Copy verbatim from the section heading; leave empty only if the "
            "heading is truly absent from the source text."
        ),
    )
    tariff_fee_item: str = Field(
        description=(
            "Official name of this fee in UPPERCASE exactly as it appears in the "
            "document heading, e.g. 'PILOTAGE SERVICES', 'PORT DUES'."
        )
    )
    port: str = Field(
        description=(
            "Port this fee applies to. Three valid values: "
            "(a) Single normalised port name e.g. 'Durban', 'Cape Town', 'Richards Bay', 'Saldanha'. "
            "(b) 'All' — fee applies to all ports or no port restriction is stated. "
            "(c) 'Other' — ONLY when the document explicitly says 'other ports' / 'remaining ports' / excluded ports. "
            "When a table has DIFFERENT numeric values per named port → emit one record per port (CASE A). "
            "When a sentence lists ports but shows no per-port rate difference → port='All' (CASE B). "
            "Never invent port names. Never convert 'Other' to 'All'."
        )
    )
    vessel_type: str = Field(
        default="All",
        description=(
            "Vessel or cargo type this fee applies to, exactly as stated in the document. "
            "Use 'All' when no vessel/cargo type restriction is stated. "
            "Emit one record per vessel_type when multiple types are listed together. "
            "Examples: 'BULK CARRIERS', 'AUTO CARRIERS', 'DRY BULK', 'BREAK BULK'. "
            "Never infer vessel type from cargo names, port names, or surrounding examples."
        ),
    )
    vessel_gt_range: str = Field(
        description=(
            "Gross tonnage band as 'gt_min-gt_max'. Both bounds mandatory. "
            "'>50 000' → '50001-999999999'; 'All ranges' → '0-999999999'; "
            "'up to 5 000' → '0-5000'. Emit one record per GT band."
        )
    )
    base_fee:                  float       = Field(default=0.0)
    incremental_fee_per_100_gt: float      = Field(default=0.0)
    formula:                   str         = Field(default="")
    conditions:                List[str]   = Field(default_factory=list)
    surcharges:                List[SurchargeItem]  = Field(default_factory=list)
    exceptions:                List[ExceptionItem]  = Field(default_factory=list)
    notes:                     Dict[str, str]       = Field(default_factory=dict)
    unmodeled_clauses:         List[str]   = Field(default_factory=list)
    extraction_confidence:     float       = Field(default=1.0)
    source_page:               int         = Field(default=0)


class PortTariffPayload(BaseModel):
    port:     str               = Field(description="Port name from document title/header.")
    country:  str               = Field(default="")
    currency: str               = Field(default="")
    fees:     List[TariffFeeItem] = Field(default_factory=list)


# ─── Gemini SDK Setup ─────────────────────────────────────────────────────────

_client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
_MODELS: List[str] = ["gemini-2.5-flash", "gemini-2.0-flash"]
_exhausted_models: set = set()
_INTER_BATCH_DELAY = 5    # seconds between section calls

_PROMPTS_DIR = BASE_DIR / "prompts"
_SP_V2_PATH  = _PROMPTS_DIR / "document_preparation_agent_sp_v2.0.md"

# Fallback to v1.8 if v2 prompt not yet written
_SYSTEM_PROMPT_V2 = (
    _SP_V2_PATH.read_text(encoding="utf-8")
    if _SP_V2_PATH.exists()
    else (_PROMPTS_DIR / "document_preparation_agent_sp_v1.8.md").read_text(encoding="utf-8")
)

_generation_config = types.GenerateContentConfig(
    system_instruction=_SYSTEM_PROMPT_V2,
    response_mime_type="application/json",
    temperature=0.0,
    max_output_tokens=65536,
)


# ─── LLM Call ─────────────────────────────────────────────────────────────────

def _parse_retry_delay(exc: Exception) -> int | None:
    m = re.search(r"['\"]retryDelay['\"]\s*:\s*['\"](\d+)s['\"]", str(exc))
    return int(m.group(1)) if m else None


def _is_daily_quota(exc: Exception) -> bool:
    return "PerDay" in str(exc)


def _extract_section_fees(
    section_markdown: str,
    section_number: str,
    section_title: str,
    max_retries: int = 3,
) -> PortTariffPayload:
    """
    Call Gemini for one section's Markdown content.
    All extraction (port, vessel_type, GT ranges, fees) is driven purely by
    the section content — no external metadata is injected into the prompt.
    """
    schema = PortTariffPayload.model_json_schema()

    section_context = (
        f"Section: {section_number or 'N/A'}  "
        f"Heading: {section_title}"
    )

    prompt = (
        f"Extract all tariff fee items from the section below.\n\n"
        f"## Section Reference\n{section_context}\n\n"
        f"## Schema\n{json.dumps(schema, indent=2)}\n\n"
        f"## Section Markdown\n\n{section_markdown}"
    )

    available = [m for m in _MODELS if m not in _exhausted_models]
    if not available:
        raise RuntimeError("All models exhausted their daily quota")

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
                if "RESOURCE_EXHAUSTED" in err_str and _is_daily_quota(exc):
                    _exhausted_models.add(model)
                    break
                if attempt == max_retries - 1:
                    raise
                suggested = _parse_retry_delay(exc)
                wait = suggested if suggested else 2 ** attempt
                log.warning(
                    f"  LLM call failed ({model}, attempt {attempt+1}): {exc} — retry in {wait}s"
                )
                time.sleep(wait)

    raise RuntimeError("All models exhausted their daily quota")


# ─── Pass 2 — Markdown Section Parser ────────────────────────────────────────

def parse_markdown_into_sections(markdown: str) -> List[Dict[str, Any]]:
    """
    Split a Markdown string into sections based on ATX heading lines.

    Returns a list of dicts:
        level   — 1–6 (number of leading #)
        number  — section number if heading starts with digits, e.g. "3.1"
        title   — heading text without the number prefix
        content — everything from this heading line up to (not including)
                  the next heading at the same or higher level
    """
    lines = markdown.split("\n")
    sections: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    buf: List[str] = []

    def _flush() -> None:
        if current is not None:
            current["content"] = "\n".join(buf)
            sections.append(current)

    for line in lines:
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            level = len(m.group(1))
            raw_title = m.group(2).strip()
            nm = _SECTION_NUM_RE.match(raw_title)
            number = nm.group(1) if nm else ""
            title  = nm.group(2).strip() if nm else raw_title

            # Close current section if same-or-higher level heading found
            if current is not None and level <= current["level"]:
                _flush()
                current = None
                buf = []

            if current is None:
                _flush()
                current = {
                    "level":   level,
                    "number":  number,
                    "title":   title,
                    "content": "",
                }
                buf = [line]
            else:
                buf.append(line)
        else:
            buf.append(line)

    _flush()
    return sections


def extract_section_content(
    full_markdown: str,
    node: SectionNode,
) -> str:
    """
    Extract the Markdown content belonging to a section node by scanning
    the full document Markdown for the matching heading line.

    Matching priority:
      1. Section number (e.g. "3.1") present in heading line
      2. Title text substring match

    Returns everything from the matching heading until the next heading at
    the same or higher level — i.e. the full sub-tree content.
    """
    lines = full_markdown.split("\n")
    start_line: Optional[int] = None
    start_level: Optional[int] = None

    for i, line in enumerate(lines):
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if not m:
            continue
        level     = len(m.group(1))
        head_text = m.group(2).strip()
        matched   = False

        # Use word-boundary anchors so "3.1" does not match inside "3.10" or "3.11"
        if node.number and re.search(
            r"(?<![.\d])" + re.escape(node.number) + r"(?![.\d])", head_text
        ):
            matched = True
        elif node.title and node.title[:30].lower() in head_text.lower():
            matched = True

        if matched:
            start_line  = i
            start_level = level
            break

    if start_line is None:
        return ""

    # Collect lines until next heading at same or higher level
    result: List[str] = []
    for i in range(start_line, len(lines)):
        line = lines[i]
        if i > start_line:
            m = re.match(r"^(#{1,6})\s+", line)
            if m and len(m.group(1)) <= start_level:
                break
        result.append(line)

    return "\n".join(result)


# ─── GT Range Parser ──────────────────────────────────────────────────────────

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


# ─── SQLite Persistence (v2 schema — adds section_name) ───────────────────────

_SCHEMA_SQL_V2 = """
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


class TariffStore:
    def __init__(self, db_path: Path) -> None:
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA_SQL_V2)
        self.conn.commit()
        log.info(f"TariffStore v2 ready: {db_path}")

    def upsert_fee_item(
        self,
        doc_id: str,
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
                item.extraction_confidence,
                item.source_page,
                now,
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

    def query_fees(
        self,
        port: str,
        gt: float,
        country: str = "",
        vessel_type: str = "",
    ) -> List[dict]:
        """
        Retrieve fee items matching port + GT range.
        Optional country and vessel_type filters narrow results further;
        both always include the 'All' catch-all bucket.
        """
        sql = """
            SELECT * FROM tariff_fee_items
            WHERE port IN (?, 'All') AND gt_min <= ? AND gt_max >= ?
        """
        params: list = [port, gt, gt]
        if country:
            sql += " AND (country = ? OR country = '')"
            params.append(country)
        if vessel_type:
            sql += " AND (vessel_type = ? OR vessel_type = 'All')"
            params.append(vessel_type)
        sql += " ORDER BY section"
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def get_ingestion_status(self, doc_id: str) -> dict:
        rows = self.conn.execute(
            "SELECT * FROM ingestion_log WHERE doc_id=? ORDER BY section_id",
            (doc_id,),
        ).fetchall()
        total_fees = self.conn.execute(
            "SELECT COUNT(*) FROM tariff_fee_items WHERE doc_id=?", (doc_id,)
        ).fetchone()[0]
        batches = [dict(r) for r in rows]
        done   = sum(1 for b in batches if b["status"] == "done")
        errors = sum(1 for b in batches if b["status"].startswith("error"))
        return {
            "doc_id": doc_id,
            "total_sections": len(batches),
            "done": done,
            "errors": errors,
            "total_fee_items": total_fees,
        }

    def close(self) -> None:
        self.conn.close()


# ─── Pass 2 — Markdown Section Traversal ─────────────────────────────────────

@dataclass
class MarkdownSection:
    """
    One ATX-heading section parsed from the Markdown file.

    direct_lines — text lines immediately under this heading, before any
                   child heading.  This is what we check for fee content.
    full_content — heading line + direct_lines + recursively all children;
                   this is what we send to the LLM.
    """
    level:        int
    number:       str           # section number e.g. "3.1", or "" if unnumbered
    title:        str           # heading text without the number prefix
    heading_line: str           # original ATX line, e.g. "## 3.1 PILOTAGE"
    direct_lines: List[str]     # lines before the first child heading
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
    """
    Parse a Markdown string into a tree of MarkdownSection nodes.

    Each ATX heading line (``# … `` through ``###### …``) starts a new
    section.  Non-heading lines accumulate in ``direct_lines`` of the
    innermost open section.  Children are sections whose ``#`` count is
    strictly greater than their parent's.
    """
    lines  = markdown.split("\n")
    roots: List[MarkdownSection] = []
    stack:  List[MarkdownSection] = []   # open sections, outermost first

    for line in lines:
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            level     = len(m.group(1))
            raw_title = m.group(2).strip()
            nm        = _SECTION_NUM_RE.match(raw_title)
            number    = nm.group(1) if nm else ""
            title     = nm.group(2).strip() if nm else raw_title

            sec = MarkdownSection(
                level=level, number=number, title=title,
                heading_line=line, direct_lines=[],
            )

            # Pop stack until we find a parent at a strictly lower level
            while stack and stack[-1].level >= level:
                stack.pop()

            if stack:
                stack[-1].children.append(sec)
            else:
                roots.append(sec)
            stack.append(sec)
        else:
            # Non-heading line → belongs to direct_lines of the innermost section
            if stack:
                stack[-1].direct_lines.append(line)

    return roots


# Minimum chars of direct content (excluding the heading line) to treat a
# section as containing fee data rather than being a pure container.
_MIN_FEE_CHARS = 60


def _has_fee_content(content: str) -> bool:
    """
    Heuristic: does this content likely contain tariff fee data?

    Returns True when the content has a Markdown table (≥2 pipe-table lines)
    or sufficient non-heading text (≥ _MIN_FEE_CHARS chars).
    """
    s = content.strip()
    if not s:
        return False
    lines = s.split("\n")
    pipe_lines = [l for l in lines if l.strip().startswith("|")]
    if len(pipe_lines) >= 2:
        return True
    non_heading = " ".join(
        l for l in lines if not re.match(r"^#{1,6}\s+", l) and l.strip()
    )
    return len(non_heading) >= _MIN_FEE_CHARS


def _find_sections_to_process(sections: List[MarkdownSection]) -> List[MarkdownSection]:
    """
    Determine which sections to send to Gemini using these rules:

    Rule A — Section has direct fee content (tables / substantive text):
        → Send the full section (heading + direct content + all children) as
          one LLM call.  The LLM handles any nested sub-section structure.

    Rule B — Section has no direct fee content but its children do:
        → Recurse into children; each qualifying child becomes a separate
          LLM call so context stays focused.

    Rule C — Neither the section nor any descendant has fee content:
        → Skip (pure container / introduction / prose-only section).

    Each qualifying section may contain multiple conditional fee rules —
    the LLM is instructed to emit one TariffFeeItem per distinct condition.
    """
    units: List[MarkdownSection] = []
    for sec in sections:
        if _has_fee_content(sec.direct_content):
            # Rule A: this section owns fee data — send it whole
            units.append(sec)
        elif sec.children:
            # Rule B: delegate to children
            child_units = _find_sections_to_process(sec.children)
            units.extend(child_units)
        # Rule C: no content, no children → nothing to send
    return units


# ─── Pass 2 — Diagnostic Tree Annotation ─────────────────────────────────────

def _annotate_section_tree(
    sections: List[MarkdownSection],
    parent_selected: bool = False,
) -> List[dict]:
    """
    Mirror the Rule A / B / C logic and return an annotated dict tree so the
    caller can log and serialise the detection result before any LLM calls.

    Each node carries:
      number               — section number or ""
      title                — heading text
      level                — ATX depth (1–6)
      rule                 — "A" | "B" | "C" | "included_in_parent"
      selected             — True when this node is a standalone LLM call unit
      direct_content_chars — char count of direct content (pre-child text)
      children             — recursively annotated child list
    """
    result: List[dict] = []
    for sec in sections:
        if parent_selected:
            # Already bundled into the parent's LLM call
            rule     = "included_in_parent"
            selected = False
            children = _annotate_section_tree(sec.children, parent_selected=True)
        elif _has_fee_content(sec.direct_content):
            # Rule A: section owns fee data directly
            rule     = "A"
            selected = True
            children = _annotate_section_tree(sec.children, parent_selected=True)
        elif sec.children:
            # Rule B: container — recurse to children
            rule     = "B"
            selected = False
            children = _annotate_section_tree(sec.children, parent_selected=False)
        else:
            # Rule C: no content, no children — skip
            rule     = "C"
            selected = False
            children = []

        result.append({
            "number":               sec.number or "",
            "title":                sec.title,
            "level":                sec.level,
            "rule":                 rule,
            "selected":             selected,
            "direct_content_chars": len(sec.direct_content),
            "children":             children,
        })
    return result


def _log_annotated_tree(nodes: List[dict], indent: int = 0) -> None:
    """Recursively log the annotated tree with visual indentation."""
    prefix = "  " * indent
    for n in nodes:
        tag    = "[SEND]" if n["selected"] else f"[{n['rule']}]"
        num    = f"{n['number']} " if n["number"] else ""
        chars  = f"  ({n['direct_content_chars']} chars)" if n["direct_content_chars"] else ""
        log.info(f"{prefix}{tag} {num}{n['title']}{chars}")
        if n["children"]:
            _log_annotated_tree(n["children"], indent + 1)


# ─── Pass 2 — Utilities ───────────────────────────────────────────────────────

def _tariff_year_from_name(name: str) -> str:
    m = re.search(r"FY[\-_]?(\d{4}[\-_]\d{2,4}|\d{4})", name, re.IGNORECASE)
    return m.group(1) if m else ""


def _doc_id(pdf_path: Path, page_start: int, page_end: Optional[int]) -> str:
    key = f"v2:{pdf_path.name}:{page_start}:{page_end or 'end'}"
    return hashlib.md5(key.encode()).hexdigest()[:12]


# ─── Pass 2 — Orchestrator ────────────────────────────────────────────────────

def run_pass2(
    md_path: Path,
    store: TariffStore,
    doc_id: str,
    tariff_year: str,
    country_override: str = "",
    version_tag: Optional[str] = None,
) -> str:
    """
    Run Pass 2 for one Markdown file:
      1. Parse the Markdown into a section tree.
      2. Select processing units via the Rule A/B/C traversal.
      3. Call Gemini once per unit for structured fee extraction.
      4. Persist results to SQLite.

    Traversal rules (applied recursively):
      A. Section has direct fee content → send full section as one LLM call.
      B. Section has no direct fee content but children do → recurse to children.
      C. Neither → skip.

    country_override — stored as the `country` column for all records.
        If empty, the LLM-extracted country from each section is used.
        Port and vessel_type are always LLM-extracted per fee item.

    Returns the version_tag used.
    """
    full_md  = md_path.read_text(encoding="utf-8")
    all_secs = parse_markdown_tree(full_md)

    # ── Step 0: diagnostic — annotate + log section tree before any LLM calls ──
    annotated = _annotate_section_tree(all_secs)
    units     = _find_sections_to_process(all_secs)

    graph_path = md_path.with_name(
        md_path.name.replace("_v2.md", "_graph.json")
    )
    graph_payload = {
        "md_file":         md_path.name,
        "doc_id":          doc_id,
        "total_top_level": len(all_secs),
        "processing_units": len(units),
        "rules": {
            "A": "Section has direct fee content → send full section as one LLM call",
            "B": "Section has no direct fee content → recurse to children",
            "C": "No fee content anywhere → skip",
            "included_in_parent": "Bundled into parent Rule-A call",
        },
        "tree": annotated,
    }
    graph_path.write_text(
        json.dumps(graph_payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    log.info("=" * 60)
    log.info(f"Pass 2 section detection — {md_path.name}")
    log.info(f"  [A]=send  [B]=recurse  [C]=skip  [included_in_parent]=bundled")
    log.info("-" * 60)
    _log_annotated_tree(annotated)
    log.info("-" * 60)
    log.info(f"  {len(units)} section(s) selected for LLM processing")
    log.info(f"  Graph written → {graph_path.name}")
    log.info("=" * 60)

    if version_tag is None:
        version_tag = tariff_year or hashlib.md5(
            f"{md_path.name}:{doc_id}".encode()
        ).hexdigest()[:8]

    log.info(
        f"Pass 2 start: {md_path.name}  "
        f"{len(units)} sections selected  doc_id={doc_id}"
    )

    skipped = 0
    for sec in tqdm(units, desc=md_path.stem, unit="section"):
        section_id = sec.section_id

        if store.is_section_done(doc_id, section_id):
            skipped += 1
            continue

        section_md = sec.full_content
        if len(section_md.strip()) < _MIN_FEE_CHARS:
            log.debug(f"  Skipping thin section '{section_id}' ({len(section_md)} chars)")
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
            log.info(
                f"  {sec.number or '—'} {sec.title[:40]}: "
                f"{len(payload.fees)} fee item(s)"
            )
        except Exception as exc:
            store.log_section(doc_id, section_id, f"error: {exc}")
            log.error(f"  Section '{section_id}' failed: {exc}")

        time.sleep(_INTER_BATCH_DELAY)

    status = store.get_ingestion_status(doc_id)
    save_active_version(version_tag)
    log.info(
        f"Pass 2 complete — version={version_tag}  "
        f"{status['total_fee_items']} fee items stored"
        + (f", {skipped} sections skipped (already done)" if skipped else "")
        + (f", {status['errors']} section(s) with errors" if status["errors"] else "")
    )
    return version_tag


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Document Preparation Agent v2 — Two-Pass PDF Extraction",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # ── Which pass(es) to run ──────────────────────────────────────────────────
    parser.add_argument(
        "--pass", dest="run_pass",
        choices=["1", "2", "all"],
        default="all",
        help="'1' = PDF→Markdown only.  '2' = Markdown→SQLite (section traversal).  "
             "'all' = run both in sequence.",
    )

    # ── PDF / page options (Pass 1) ────────────────────────────────────────────
    parser.add_argument(
        "--page-start", type=int, default=1, metavar="N",
        help="First PHYSICAL PDF page to process (1-indexed, as numbered in the "
             "PDF file structure — not the page number printed in the document). "
             "For two-column/booklet scans each physical page holds two content "
             "pages, so physical page N covers content pages (2N-1) and (2N).",
    )
    parser.add_argument(
        "--page-end", type=int, default=None, metavar="N",
        help="Last PHYSICAL PDF page to process (1-indexed, inclusive). "
             "Same physical-vs-content page distinction as --page-start.",
    )
    parser.add_argument(
        "--two-column-layout", action="store_true", default=False,
        help="Each physical PDF page contains two side-by-side document pages "
             "(scanned booklet). pymupdf4llm handles the column split automatically. "
             "Use physical page numbers with --page-start/--page-end.",
    )
    parser.add_argument(
        "--toc-page-end", type=int, default=None, metavar="N",
        help="Last PHYSICAL PDF page sent to Gemini for TOC extraction (strategy 2). "
             "Default: page-start + 14 (first 15 physical pages). Increase if your "
             "document's Table of Contents spans more pages.",
    )

    # ── Pass 2 input paths (when --pass 2 is used standalone) ────────────────
    parser.add_argument("--md-path", type=Path, default=None, metavar="PATH",
                        help="Markdown file from Pass 1.  Auto-discovered when --pass all.")

    # ── Metadata stored as SQLite filter columns (not injected into LLM) ────────
    parser.add_argument(
        "--country", default="", metavar="NAME",
        help="Country name stored as the `country` column on every record in this run, "
             "e.g. 'South Africa'.  Overrides the country the LLM may extract from "
             "the document.  Useful for consistent query-time filtering.",
    )

    # ── Version tagging ───────────────────────────────────────────────────────
    parser.add_argument("--version-tag", default=None, metavar="TAG",
                        help="Version label, e.g. 'FY2025-26'.  "
                             "Data is stored in tariff_store_v2_<TAG>.db.")

    args = parser.parse_args()

    pdf_files = sorted(RAW_DIR.glob("*.pdf"))
    if not pdf_files:
        log.error(f"No PDF files found in {RAW_DIR}")
        raise SystemExit(1)
    log.info(f"Found {len(pdf_files)} PDF(s) in {RAW_DIR}")

    for pdf_path in pdf_files:
        tariff_year = _tariff_year_from_name(pdf_path.name)
        version_tag = args.version_tag or tariff_year or pdf_path.stem

        # ── Pass 1 ──────────────────────────────────────────────────────────
        if args.run_pass in ("1", "all"):
            md_path = run_pass1(
                pdf_path,
                page_start=args.page_start,
                page_end=args.page_end,
                two_column_layout=args.two_column_layout,
                toc_page_end=args.toc_page_end,
            )
        else:
            # Standalone Pass 2 — user must supply --md-path
            md_path = args.md_path
            if not md_path:
                log.error("--pass 2 requires --md-path")
                raise SystemExit(1)

        # ── Pass 2 ──────────────────────────────────────────────────────────
        if args.run_pass in ("2", "all"):
            target_db = db_path_for_version(version_tag)
            store     = TariffStore(target_db)
            did       = _doc_id(pdf_path, args.page_start, args.page_end)
            try:
                used_tag = run_pass2(
                    md_path,
                    store,
                    doc_id=did,
                    tariff_year=tariff_year,
                    country_override=args.country,
                    version_tag=version_tag,
                )
                log.info(f"Active v2 version set to: {used_tag}")
            finally:
                store.close()
