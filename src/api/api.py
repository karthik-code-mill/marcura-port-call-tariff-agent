"""
Tariff Calculation API — v3.1
==============================
FastAPI entry point for the full two-stage tariff pipeline.

Stage 1 — Document Preparation  (§2.2)
  POST  /api/v1/prepare              Trigger PDF ingestion (parse → extract → validate)
  GET   /api/v1/status/{doc_id}      Ingestion status + validation summary
  GET   /api/v1/holds                View validation holds (on-hold fee records)
  GET   /api/v1/fees                 Query extracted fee items (port + GT filter)
  GET   /api/v1/files                List available PDFs in the raw directory
  GET   /api/v1/versions             List all tariff DB versions
  POST  /api/v1/versions/activate    Switch the active tariff version

Stage 2 — Tariff Execution  (§2.3)
  POST  /api/v2/calculate            Compute a full tariff invoice for a vessel

Observability
  GET   /api/v1/metrics              In-process business metrics snapshot

Run:
    uvicorn api.api:app --reload --port 8000 --app-dir src
"""

import asyncio
import json
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))  # project root for guardrails

from agents.document_preparation_agent import (
    RAW_DIR,
    TariffStore,
    _doc_id,
    _tariff_year_from_name,
    db_path_for_version,
    list_available_versions,
    load_active_version,
    md_path_for,
    raw_dir_for,
    save_active_version,
)
from agents.hierarchical_chunk_agent import CHUNK_DB_PATH, ChunkStore
from guardrails.vessel_input_guardrail import VesselInputGuardrailError
from models.config import TariffPipelineConfig
from models.invoice import TariffInvoice
from models.vessel import VesselInput
from monitoring.business_metrics import metrics
from orchestrator import run_pipeline
from orchestrator.document_prep_pipeline import run_document_prep_pipeline

log = logging.getLogger(__name__)

_DEFAULT_COUNTRY = "South Africa"

# Validation holds written by the Validation Agent during Stage 1.
_HOLDS_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "config" / "validation_holds.json"
)


# ─── App lifecycle ────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Load the active tariff store and chunk store on startup.
    Per-request calculate calls open a fresh store so version switching
    takes effect immediately without a server restart.
    """
    active_tag = load_active_version(_DEFAULT_COUNTRY)
    if active_tag:
        active_db = db_path_for_version(_DEFAULT_COUNTRY, active_tag)
        app.state.tariff_store   = TariffStore(active_db)
        app.state.active_version = active_tag
        app.state.active_country = _DEFAULT_COUNTRY
        log.info(f"[API] Loaded tariff store: {_DEFAULT_COUNTRY} / {active_tag}")
    else:
        app.state.tariff_store   = None
        app.state.active_version = None
        app.state.active_country = _DEFAULT_COUNTRY
        log.warning("[API] No active tariff version — run POST /api/v1/prepare then /api/v1/versions/activate")

    app.state.chunk_store = ChunkStore(CHUNK_DB_PATH) if CHUNK_DB_PATH.exists() else None

    # In-process job tracker: doc_id → {status, message, extraction_summary, validation_summary, errors}
    # Cleared on server restart — the DB ingestion_log is the persistent source of truth.
    app.state.jobs: Dict[str, dict] = {}

    yield

    if app.state.tariff_store:
        app.state.tariff_store.close()
    if app.state.chunk_store:
        app.state.chunk_store.close()


app = FastAPI(
    title="Automated Vessel Tariff Calculation API",
    description=(
        "**Stage 1 (§2.2):** Ingest port tariff PDFs — parse → extract → validate.\n\n"
        "**Stage 2 (§2.3):** Compute a fully itemized vessel tariff invoice.\n\n"
        "Every invoice line is traceable to a source page and extraction confidence score."
    ),
    version="3.1.0",
    lifespan=lifespan,
)


# ─── Global exception handlers ────────────────────────────────────────────────

@app.exception_handler(VesselInputGuardrailError)
async def _vessel_guardrail_handler(req: Request, exc: VesselInputGuardrailError) -> JSONResponse:
    """Return 422 with structured violation detail when vessel payload fails hard validation."""
    violations = [
        {
            "field":    v.field,
            "message":  v.message,
            "severity": "error" if v.hard else "warning",
        }
        for v in exc.result.violations
    ]
    return JSONResponse(
        status_code=422,
        content={
            "error":      "vessel_input_validation_failed",
            "detail":     str(exc),
            "violations": violations,
        },
    )


# ─── Request / Response models ────────────────────────────────────────────────

class PrepareRequest(BaseModel):
    filename:          str   = Field(description="PDF filename inside the raw input directory.")
    country:           str   = Field(default=_DEFAULT_COUNTRY)
    version_tag:       Optional[str] = Field(
        default=None,
        description="Version tag to assign (e.g. 'FY2025-26-v3.2'). Inferred from filename if omitted.",
    )
    page_start:        int   = Field(default=1, ge=1, description="First PDF page to parse (1-indexed).")
    page_end:          Optional[int] = Field(default=None, ge=1, description="Last PDF page to parse. Omit for full document.")
    two_column_layout: bool  = Field(
        default=False,
        description="Enable two-column PDF reflow in pymupdf4llm. Set True for multi-column tariff layouts.",
    )
    skip_parse:        bool  = Field(
        default=False,
        description=(
            "Skip PDF→Markdown conversion and use the existing Markdown file. "
            "Useful for re-running extraction after a quota error without re-parsing the PDF."
        ),
    )
    delete_after_parse: bool = Field(
        default=False,
        description=(
            "Delete the source PDF from the raw directory once Markdown has been "
            "successfully written to disk. Deletion failure is non-fatal — the pipeline "
            "continues and a warning is logged. Has no effect when skip_parse=true."
        ),
    )

    model_config = {"json_schema_extra": {"example": {
        "filename":    "Publisher-Tariff-Book-FY-2025-26.pdf",
        "country":     "South Africa",
        "version_tag": "FY2025-26-v3.2",
        "page_start":  5,
    }}}


class JobResponse(BaseModel):
    doc_id:   str
    filename: str
    status:   str
    message:  str


class ActivateVersionRequest(BaseModel):
    country:     str = _DEFAULT_COUNTRY
    version_tag: str

    model_config = {"json_schema_extra": {"example": {
        "country":     "South Africa",
        "version_tag": "FY2025-26-v3.2",
    }}}


class CalculateRequest(BaseModel):
    vessel: VesselInput = Field(description="Vessel details for tariff computation.")
    tariff_version: Optional[str] = Field(
        default=None,
        description=(
            "Specific version tag to use (e.g. 'FY2025-26-v3.2'). "
            "Defaults to the active version for the vessel's country."
        ),
    )

    model_config = {"json_schema_extra": {"example": {
        "vessel": {
            "country":            "South Africa",
            "port":               "Durban",
            "gross_tonnage":      51300,
            "vessel_type":        "Bulk Carrier",
            "voyage_type":        "inbound",
            "cargo_type":         "Iron Ore",
            "after_hours":        False,
            "public_holiday":     False,
            "in_ballast":         False,
            "special_conditions": [],
        },
        "tariff_version": "FY2025-26-v3.2",
    }}}


# ─── Shared helpers ───────────────────────────────────────────────────────────

def _resolve_pdf(filename: str, country: str = "") -> Path:
    """Locate a PDF by filename.

    Search order:
      1. Country-specific subdirectory  — context-layer/rag/raw/{country_slug}/{filename}
      2. Root raw directory (legacy)    — context-layer/rag/raw/{filename}

    The root fallback keeps backward compatibility with PDFs placed there before
    per-country subdirectories were introduced.
    """
    if country:
        country_path = raw_dir_for(country) / filename
        if country_path.exists():
            return country_path

    root_path = RAW_DIR / filename
    if root_path.exists():
        return root_path

    # Collect available filenames from both locations for a helpful error message.
    available: list[str] = []
    if country:
        available += [f.name for f in sorted(raw_dir_for(country).glob("*.pdf"))]
    available += [f.name for f in sorted(RAW_DIR.glob("*.pdf"))]
    available = list(dict.fromkeys(available))   # deduplicate while preserving order

    raise HTTPException(
        status_code=404,
        detail={"error": f"'{filename}' not found.", "available": available},
    )


def _require_tariff_store(req: Request) -> TariffStore:
    if req.app.state.tariff_store is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "No active tariff store loaded. "
                "Run POST /api/v1/prepare then POST /api/v1/versions/activate."
            ),
        )
    return req.app.state.tariff_store


def _read_holds() -> dict:
    if not _HOLDS_PATH.exists():
        return {}
    try:
        return json.loads(_HOLDS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ─── Root ─────────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def root() -> dict:
    return {
        "service": "Automated Vessel Tariff Calculation API",
        "version": "3.1.0",
        "docs":    "/docs",
        "redoc":   "/redoc",
    }


# ─── Stage 1: Document Preparation ───────────────────────────────────────────

@app.get(
    "/api/v1/files",
    summary="List available PDF files",
    tags=["Stage 1 — Document Preparation"],
)
async def list_files(
    country: str = Query(
        default="",
        description=(
            "Filter PDFs by country slug. Omit to list all PDFs across every country "
            "subdirectory and the root raw directory."
        ),
    ),
) -> dict:
    """
    List PDF files available for ingestion.

    PDFs uploaded via `POST /api/v1/upload` land in a country-specific subdirectory
    (`context-layer/rag/raw/{country_slug}/`).  PDFs placed manually in the root
    `context-layer/rag/raw/` directory are listed under the `_root` key.

    Passing `country` returns only that country's files.
    """
    if country:
        files = [f.name for f in sorted(raw_dir_for(country).glob("*.pdf"))]
        return {"country": country, "files": files, "count": len(files)}

    # No filter — aggregate across all country subdirs and the root dir.
    files_by_country: Dict[str, List[str]] = {}
    for sub in sorted(RAW_DIR.iterdir()):
        if sub.is_dir():
            pdfs = [f.name for f in sorted(sub.glob("*.pdf"))]
            if pdfs:
                files_by_country[sub.name] = pdfs
    root_pdfs = [f.name for f in sorted(RAW_DIR.glob("*.pdf"))]
    if root_pdfs:
        files_by_country["_root"] = root_pdfs

    total = sum(len(v) for v in files_by_country.values())
    return {"files_by_country": files_by_country, "total": total}


@app.get(
    "/api/v1/versions",
    summary="List all tariff DB versions",
    tags=["Stage 1 — Document Preparation"],
)
async def get_versions(country: str = Query(_DEFAULT_COUNTRY)) -> List[dict]:
    """
    List every extracted tariff version for a country.
    Each entry includes `is_active`, `db_exists`, and the full DB path.
    """
    return list_available_versions(country)


@app.post(
    "/api/v1/versions/activate",
    summary="Switch the active tariff version",
    tags=["Stage 1 — Document Preparation"],
)
async def activate_version(body: ActivateVersionRequest) -> dict:
    """
    Update `app_config.json` to set the active tariff version for a country.

    The in-memory tariff store (loaded at startup) is **not** hot-reloaded.
    Pass `tariff_version` explicitly in `/api/v2/calculate` to use the new
    version without restarting the server.
    """
    versions = [v["version_tag"] for v in list_available_versions(body.country)]
    if body.version_tag not in versions:
        raise HTTPException(
            status_code=404,
            detail=f"Version '{body.version_tag}' not found for '{body.country}'. Available: {versions}",
        )
    save_active_version(body.country, body.version_tag)
    return {
        "country":        body.country,
        "active_version": body.version_tag,
        "message":        (
            "Active version updated in app_config.json. "
            "Pass tariff_version explicitly in /api/v2/calculate to use it immediately."
        ),
    }


@app.post(
    "/api/v1/upload",
    status_code=201,
    summary="Upload a tariff book PDF",
    tags=["Stage 1 — Document Preparation"],
)
async def upload_pdf(
    file: UploadFile = File(..., description="PDF file to upload."),
    country: str = Query(
        default=_DEFAULT_COUNTRY,
        description="Country this tariff book belongs to (e.g. 'South Africa').",
    ),
    overwrite: bool = Query(
        default=False,
        description="Replace the file if it already exists. Defaults to false.",
    ),
) -> dict:
    """
    Upload a tariff book PDF for a specific country.

    The file is saved to `context-layer/rag/raw/{country_slug}/{filename}`.
    Use the returned `filename` in the `POST /api/v1/prepare` request body to
    trigger ingestion.

    **Validation:**
    - File extension must be `.pdf`
    - Content-Type must be `application/pdf` or `application/octet-stream`
    - First four bytes must be `%PDF` (PDF magic header)

    **Error codes:**
    | Code | Reason |
    |------|--------|
    | 400  | Not a PDF, wrong extension, or corrupt file |
    | 409  | File already exists and `overwrite=false` |
    | 500  | Disk write failure |
    """
    # Validate extension
    original_name = (file.filename or "upload.pdf").strip()
    if not original_name.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="File must have a .pdf extension.")

    # Validate content-type (browsers may send application/octet-stream for .pdf)
    if file.content_type not in ("application/pdf", "application/octet-stream", None):
        raise HTTPException(
            status_code=400,
            detail=f"Expected a PDF. Received content-type: {file.content_type}",
        )

    # Strip any path components from the browser-supplied filename
    safe_name = Path(original_name).name

    dest_dir  = raw_dir_for(country)
    dest_path = dest_dir / safe_name

    if dest_path.exists() and not overwrite:
        raise HTTPException(
            status_code=409,
            detail=(
                f"'{safe_name}' already exists for country '{country}'. "
                "Pass overwrite=true to replace it."
            ),
        )

    # Read fully so we can check the magic bytes before touching the disk.
    content = await file.read()

    if not content.startswith(b"%PDF"):
        raise HTTPException(
            status_code=400,
            detail="File does not appear to be a valid PDF (missing %PDF header).",
        )

    try:
        dest_path.write_bytes(content)
    except OSError as exc:
        log.error(f"[API] Failed to write uploaded PDF {safe_name}: {exc}")
        raise HTTPException(status_code=500, detail=f"Failed to save file: {exc}") from exc

    size_kb = len(content) / 1024
    log.info(f"[API] PDF uploaded — {safe_name} ({size_kb:.1f} KB) → {dest_path}")

    return {
        "filename":    safe_name,
        "country":     country,
        "size_bytes":  len(content),
        "saved_to":    str(dest_path),
        "next_step":   f"POST /api/v1/prepare with filename='{safe_name}' and country='{country}'",
    }


@app.post(
    "/api/v1/prepare",
    response_model=JobResponse,
    status_code=202,
    summary="Ingest a tariff PDF — parse → extract → validate",
    tags=["Stage 1 — Document Preparation"],
)
async def trigger_prepare(body: PrepareRequest, background_tasks: BackgroundTasks) -> JobResponse:
    """
    Trigger the full Stage-1 document preparation pipeline for a PDF file.

    **Pipeline steps (LangGraph-orchestrated):**
    1. **Parser Agent** — PDF → Markdown using pymupdf4llm (layout-aware)
    2. **Rule Extractor Agent** — Markdown → SQLite fee records, one LLM call per section
    3. **Validation Agent** — DB cross-check; records below confidence 0.6 or with
       unmodeled clauses are re-validated. On-hold items are written to `validation_holds.json`.

    Set `skip_parse=true` to reuse an existing Markdown file (e.g., after a quota error
    interrupted Step 2 without needing to re-parse the PDF).

    Poll `GET /api/v1/status/{doc_id}` to track progress.
    """
    pdf_path    = _resolve_pdf(body.filename, country=body.country)
    tariff_year = _tariff_year_from_name(pdf_path.name)
    version_tag = body.version_tag or tariff_year or pdf_path.stem
    did         = _doc_id(pdf_path, body.page_start, body.page_end)

    # Resolve the expected Markdown path when skip_parse is requested.
    existing_md: Optional[Path] = None
    if body.skip_parse:
        existing_md = md_path_for(body.country, version_tag)
        if not existing_md.exists():
            raise HTTPException(
                status_code=400,
                detail=(
                    f"skip_parse=true but no Markdown file found at: {existing_md}. "
                    "Run without skip_parse first to produce the Markdown."
                ),
            )

    app.state.jobs[did] = {
        "status":             "queued",
        "message":            f"Pipeline queued for {body.country} / {version_tag}.",
        "extraction_summary": None,
        "validation_summary": None,
        "errors":             [],
    }

    async def _run() -> None:
        app.state.jobs[did]["status"]  = "running"
        app.state.jobs[did]["message"] = "Pipeline running…"
        try:
            final = await asyncio.to_thread(
                run_document_prep_pipeline,
                pdf_path=pdf_path,
                country=body.country,
                version_tag=version_tag,
                page_start=body.page_start,
                page_end=body.page_end,
                two_column_layout=body.two_column_layout,
                skip_parse=body.skip_parse,
                md_path=existing_md,
                delete_after_parse=body.delete_after_parse,
            )
            ext  = final.get("extraction_summary")
            val  = final.get("validation_summary")
            errs = final.get("errors", [])
            app.state.jobs[did].update({
                "status":  "error" if errs else "done",
                "message": (
                    f"Done — "
                    f"{ext.fee_items_written if ext else 0} fee items extracted, "
                    f"{val.on_hold_count if val else 0} on hold."
                ),
                "extraction_summary": ext.model_dump() if ext else None,
                "validation_summary": val.model_dump() if val else None,
                "errors": errs,
            })
            log.info(f"[API] Prepare complete: {did}  status={app.state.jobs[did]['status']}")
        except Exception as exc:
            log.error(f"[API] Prepare pipeline failed for {did}: {exc}", exc_info=True)
            app.state.jobs[did].update({
                "status":  "error",
                "message": f"Pipeline failed: {exc}",
                "errors":  [str(exc)],
            })

    background_tasks.add_task(_run)
    return JobResponse(
        doc_id=did,
        filename=body.filename,
        status="queued",
        message=f"Pipeline queued for {body.country} / {version_tag}. Poll /api/v1/status/{did}.",
    )


@app.get(
    "/api/v1/status/{doc_id}",
    summary="Ingestion status and pipeline summary",
    tags=["Stage 1 — Document Preparation"],
)
async def get_status(doc_id: str, req: Request) -> dict:
    """
    Returns the current state of a document preparation job.

    **Response sections:**
    - `pipeline_job` — in-process job state (extraction + validation summaries when done; cleared on restart)
    - `fee_extraction` — DB ingestion log (persisted in SQLite, survives restart)
    - `chunk_extraction` — chunk store ingestion log (if built)
    - `validation_holds` — count and keys of on-hold records for this doc in `validation_holds.json`
    """
    tariff_store = _require_tariff_store(req)
    fee_status   = tariff_store.get_ingestion_status(doc_id)

    holds     = _read_holds()
    doc_holds = holds.get(doc_id, {})

    chunk_status = (
        req.app.state.chunk_store.get_ingestion_status(doc_id)
        if req.app.state.chunk_store
        else {"status": "chunk_store_not_built"}
    )

    job = req.app.state.jobs.get(doc_id, {
        "status":  "unknown",
        "message": "No in-process record found — server may have restarted. See fee_extraction for DB state.",
    })

    return {
        "doc_id":           doc_id,
        "pipeline_job":     job,
        "fee_extraction":   fee_status,
        "chunk_extraction": chunk_status,
        "validation_holds": {
            "count": len(doc_holds),
            "items": list(doc_holds.keys()),
        },
    }


@app.get(
    "/api/v1/holds",
    summary="View validation holds",
    tags=["Stage 1 — Document Preparation"],
)
async def get_holds(
    doc_id: Optional[str] = Query(
        default=None,
        description="Filter by doc_id. Omit to return holds across all documents.",
    ),
) -> dict:
    """
    Returns fee records currently flagged **on_hold** by the Validation Agent.

    On-hold records have `extraction_confidence = 0.0` in the DB and are
    automatically blocked by the Retrieval Confidence Guardrail at calculation
    time — they do **not** contribute to any invoice until a human reviews and
    clears them.

    Clearing a hold requires:
    1. Correcting the extraction manually or re-running Stage 1 with updated prompt.
    2. Removing the entry from `validation_holds.json`.
    3. Updating `extraction_confidence` in the DB to a value ≥ 0.2.
    """
    holds = _read_holds()
    if doc_id:
        return {
            "doc_id": doc_id,
            "count":  len(holds.get(doc_id, {})),
            "holds":  holds.get(doc_id, {}),
        }
    total = sum(len(v) for v in holds.values())
    return {
        "total_holds": total,
        "by_doc":      holds,
    }


@app.get(
    "/api/v1/fees",
    summary="Query extracted fee items",
    tags=["Stage 1 — Document Preparation"],
)
async def query_fees(
    req:     Request,
    port:    str   = Query(..., description="Port name exactly as stored, e.g. 'Durban'"),
    gt:      float = Query(..., description="Vessel gross tonnage"),
    country: str   = Query(_DEFAULT_COUNTRY),
) -> List[dict]:
    """
    Query the active tariff DB for fee records matching a port and gross tonnage.

    Returns named-port rows (highest priority), national 'All' rows, and 'Other'
    fallback rows in section order — the same three-tier lookup the Retriever Agent uses.
    """
    store = _require_tariff_store(req)
    rows  = store.conn.execute(
        """
        SELECT * FROM tariff_fee_items
        WHERE port IN (?, 'All', 'Other')
          AND gt_min <= ? AND gt_max >= ?
          AND (country = ? OR country = '')
        ORDER BY
            CASE port WHEN ? THEN 0 WHEN 'All' THEN 1 ELSE 2 END,
            section
        """,
        (port, gt, gt, country, port),
    ).fetchall()
    return [dict(r) for r in rows]


# ─── Stage 2: Tariff Execution ────────────────────────────────────────────────

@app.post(
    "/api/v2/calculate",
    response_model=TariffInvoice,
    summary="Compute a full tariff invoice for a vessel",
    tags=["Stage 2 — Tariff Execution"],
)
async def calculate_tariff(body: CalculateRequest) -> TariffInvoice:
    """
    Run the Stage-2 LangGraph pipeline and return a fully itemized tariff invoice.

    **Pipeline steps:**
    1. **Vessel Input Guardrail** — validates the payload; returns 422 on hard violations
       (empty port, non-positive GT, unknown voyage_type, GT > 500 000).
    2. **Retriever Agent** — three-tier SQL filter → validation-holds filter →
       extraction-confidence guardrail → LLM applicability evaluation.
    3. **Calculator Agent** — deterministic Python formula eval → calculation guardrail
       (negative/extreme value checks) → LLM audit pass → invoice assembly.

    Items that cannot be computed safely are included in the invoice with
    `needs_human_review=true` and a `review_reason`. The pipeline never silently
    drops a fee item.

    **Error codes:**
    | Code | Meaning |
    |------|---------|
    | 422  | Vessel payload failed hard validation — see `violations` in body |
    | 404  | Tariff DB not found for the requested country/version |
    | 503  | No active tariff store loaded on the server |
    | 500  | Unexpected pipeline error |
    """
    country = body.vessel.country
    cfg     = TariffPipelineConfig(country=country, tariff_version=body.tariff_version)

    try:
        req_db = cfg.resolved_db_path
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    if not req_db.exists():
        raise HTTPException(
            status_code=404,
            detail=(
                f"Tariff DB not found: '{req_db.name}'. "
                "Run POST /api/v1/prepare to ingest the tariff document first."
            ),
        )

    req_store = TariffStore(req_db)
    try:
        invoice = await asyncio.to_thread(
            run_pipeline,
            body.vessel,
            req_store,
            app.state.chunk_store,
            cfg,
        )
        metrics.log_snapshot()
        return invoice
    except VesselInputGuardrailError:
        raise   # caught by the registered exception handler → 422
    except Exception as exc:
        log.error(f"[API] calculate_tariff pipeline error: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Pipeline error: {exc}") from exc
    finally:
        req_store.close()


# ─── Observability ────────────────────────────────────────────────────────────

@app.get(
    "/api/v1/metrics",
    summary="Business metrics snapshot",
    tags=["Observability"],
)
async def get_metrics() -> dict:
    """
    Returns the current in-process business metrics snapshot.

    Counters are accumulated since the last server start and cover:
    - Retrieval hit/miss rates
    - Formula evaluation errors
    - Per-guardrail rejection counts
    - Cumulative LLM token usage per agent
    - Fee accuracy against benchmark amounts (if `record_fee_accuracy` has been called)

    **Note:** Counters reset on server restart. For persistent metrics, integrate
    with Azure Monitor or Prometheus — see `src/monitoring/business_metrics.py`.
    """
    return metrics.snapshot()
