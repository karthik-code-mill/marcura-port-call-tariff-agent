"""
Tariff Calculation API
======================
FastAPI entry point for the full two-stage tariff pipeline.

Stage 1 — Document Preparation (§2.2)
  POST  /api/v1/prepare              Trigger §2.2.1 fee extraction for a PDF
  POST  /api/v1/chunk                Trigger §2.2.2 hierarchical chunking for a PDF
  GET   /api/v1/status/{doc_id}      Combined status from both stores
  GET   /api/v1/fees                 Query extracted fee items (port + GT filter)
  GET   /api/v1/chunks               Query hierarchical chunks (fee item + type filter)
  GET   /api/v1/chunks/search        Full-text search across chunk content
  GET   /api/v1/files                List available PDFs in the raw directory
  GET   /api/v1/versions             List all tariff DB versions
  POST  /api/v1/versions/activate    Switch the active tariff version

Stage 2 — Execution (§2.3)
  POST  /api/v2/calculate            Compute a full tariff invoice for a vessel
                                     (optional tariff_version in request body)

Stage-1 agents are dispatched as BackgroundTasks (long-running PDF + LLM calls).
Stage-2 calculate runs synchronously in a thread pool — the caller waits for the result.

Run:
    uvicorn src.api:app --reload --port 8000
    # or from project root:
    uvicorn api:app --reload --port 8000 --app-dir src
"""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from pydantic import BaseModel

load_dotenv()

# ─── Import agents and stores ─────────────────────────────────────────────────

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))

from document_preparation_agent import (
    DB_PATH,
    RAW_DIR,
    TariffStore,
    db_path_for_version,
    doc_id_from_path,
    list_available_versions,
    load_active_version,
    run_document_preparation,
    save_active_version,
)
from hierarchical_chunk_agent import (
    CHUNK_DB_PATH,
    ChunkStore,
    run_hierarchical_chunking,
)
from models.config import TariffPipelineConfig
from models.vessel import VesselInput
from models.invoice import TariffInvoice
from orchestrator import run_pipeline

# ─── App Lifecycle ────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    active_tag = load_active_version()
    active_db  = db_path_for_version(active_tag) if active_tag else DB_PATH
    app.state.tariff_store   = TariffStore(active_db)
    app.state.chunk_store    = ChunkStore(CHUNK_DB_PATH) if CHUNK_DB_PATH.exists() else None
    app.state.active_version = active_tag
    yield
    app.state.tariff_store.close()
    if app.state.chunk_store:
        app.state.chunk_store.close()


app = FastAPI(
    title="Automated Vessel Tariff Calculation API",
    description=(
        "Stage-1 (§2.2): ingest port tariff PDFs, extract structured fee data and chunk index.\n\n"
        "Stage-2 (§2.3): compute a fully itemised tariff invoice for a vessel at a port."
    ),
    version="2.0.0",
    lifespan=lifespan,
)

# ─── Request / Response Models ────────────────────────────────────────────────

class PrepareRequest(BaseModel):
    filename: str

    model_config = {"json_schema_extra": {"example": {"filename": "Publisher-Tariff-Book-FY-2025-26.pdf"}}}


class JobResponse(BaseModel):
    doc_id: str
    filename: str
    status: str
    message: str


class StatusResponse(BaseModel):
    doc_id: str
    fee_extraction: dict
    chunk_extraction: dict


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _resolve_pdf(filename: str) -> Path:
    pdf_path = RAW_DIR / filename
    if not pdf_path.exists():
        available = [f.name for f in sorted(RAW_DIR.glob("*.pdf"))]
        raise HTTPException(
            status_code=404,
            detail={"error": f"'{filename}' not found in {RAW_DIR}", "available": available},
        )
    return pdf_path


def _get_tariff_store(app) -> TariffStore:
    return app.state.tariff_store


def _get_chunk_store(app) -> ChunkStore:
    if app.state.chunk_store is None:
        raise HTTPException(
            status_code=503,
            detail="Chunk store not available. Run POST /api/v1/chunk to build the hierarchical index first.",
        )
    return app.state.chunk_store


# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def root():
    return {"service": "Tariff Document Preparation API", "docs": "/docs"}


@app.get("/api/v1/files", summary="List available PDF files")
async def list_files() -> List[str]:
    """Returns the names of all PDF files in the raw document directory."""
    return [f.name for f in sorted(RAW_DIR.glob("*.pdf"))]


# ─── Version Management ───────────────────────────────────────────────────────

@app.get("/api/v1/versions", summary="List all tariff database versions")
async def get_versions() -> List[dict]:
    """
    Returns all registered tariff versions from pipeline_config.json.
    Each entry shows the version tag, DB path, whether the DB file exists,
    and which version is currently active.
    """
    return list_available_versions()


class ActivateVersionRequest(BaseModel):
    version_tag: str

    model_config = {"json_schema_extra": {"example": {"version_tag": "FY2025-26"}}}


@app.post("/api/v1/versions/activate", summary="Switch the active tariff version")
async def activate_version(request: ActivateVersionRequest) -> dict:
    """
    Sets the named version as active in pipeline_config.json.

    **Note:** The running API server will continue using the store it opened at
    startup until it is restarted.  Use this endpoint to switch the default for
    subsequent CLI runs or new API instances.
    """
    versions = [v["version_tag"] for v in list_available_versions()]
    if request.version_tag not in versions:
        raise HTTPException(
            status_code=404,
            detail=f"Version '{request.version_tag}' not found. Available: {versions}",
        )
    save_active_version(request.version_tag)
    return {"active_version": request.version_tag, "message": "Active version updated in pipeline_config.json."}


@app.post(
    "/api/v1/prepare",
    response_model=JobResponse,
    summary="Trigger §2.2.1 fee extraction",
    status_code=202,
)
async def trigger_prepare(
    request: PrepareRequest,
    background_tasks: BackgroundTasks,
) -> JobResponse:
    """
    Dispatch the document preparation agent for the given PDF.
    Returns immediately with a doc_id — poll /api/v1/status/{doc_id} for progress.

    The pipeline is resumable: re-posting the same filename skips already-completed
    page batches and only processes any that previously failed.
    """
    pdf_path    = _resolve_pdf(request.filename)
    doc_id      = doc_id_from_path(pdf_path)
    tariff_store = app.state.tariff_store

    background_tasks.add_task(run_document_preparation, pdf_path, tariff_store)

    return JobResponse(
        doc_id=doc_id,
        filename=request.filename,
        status="queued",
        message="Fee extraction started in background. Poll /api/v1/status/{doc_id} for progress.",
    )


@app.post(
    "/api/v1/chunk",
    response_model=JobResponse,
    summary="Trigger §2.2.2 hierarchical chunking",
    status_code=202,
)
async def trigger_chunk(
    request: PrepareRequest,
    background_tasks: BackgroundTasks,
) -> JobResponse:
    """
    Dispatch the hierarchical chunk agent for the given PDF.
    Produces an independent data source in hierarchical_store.db.

    Can run concurrently with or after /api/v1/prepare.
    If fee extraction has already run, chunk fee_item_name fields will be
    auto-enriched from the tariff_store section map.
    """
    pdf_path    = _resolve_pdf(request.filename)
    doc_id      = doc_id_from_path(pdf_path)
    chunk_store = app.state.chunk_store

    background_tasks.add_task(run_hierarchical_chunking, pdf_path, chunk_store)

    return JobResponse(
        doc_id=doc_id,
        filename=request.filename,
        status="queued",
        message="Hierarchical chunking started in background. Poll /api/v1/status/{doc_id} for progress.",
    )


@app.get(
    "/api/v1/status/{doc_id}",
    response_model=StatusResponse,
    summary="Combined pipeline status",
)
async def get_status(doc_id: str) -> StatusResponse:
    """
    Returns batch-level progress from both the tariff_store and chunk_store
    for the given doc_id.
    """
    chunk_status = (
        app.state.chunk_store.get_ingestion_status(doc_id)
        if app.state.chunk_store else {"status": "not_built"}
    )
    return StatusResponse(
        doc_id=doc_id,
        fee_extraction=app.state.tariff_store.get_ingestion_status(doc_id),
        chunk_extraction=chunk_status,
    )


@app.get(
    "/api/v1/fees",
    summary="Query extracted fee items (§2.3.1 retriever input)",
)
async def query_fees(
    port: str = Query(..., description="Port name e.g. 'Durban'"),
    gt: float = Query(..., description="Vessel gross tonnage e.g. 51300"),
) -> List[dict]:
    """
    Returns all fee items where gt_min <= gt <= gt_max for the given port.
    This is the primary data source for the Stage-2 Retriever Agent.
    """
    return app.state.tariff_store.query_fees(port=port, gt=gt)


@app.get(
    "/api/v1/chunks",
    summary="Query hierarchical chunks by fee item and/or type",
)
async def query_chunks(
    fee_item: Optional[str] = Query(None, description="Fee item name e.g. 'PILOTAGE SERVICES'"),
    chunk_type: Optional[str] = Query(
        None,
        description=(
            "Chunk type filter: section_intro | rule | table | conditions | "
            "exceptions | formula | notes | definition | references | unclassified"
        ),
    ),
    section: Optional[str] = Query(None, description="Section number e.g. '3.3'"),
) -> List[dict]:
    """
    Query the hierarchical chunk index by fee item name, chunk type, or section.

    Examples:
    - All exception clauses for pilotage: fee_item=PILOTAGE SERVICES&chunk_type=exceptions
    - All chunks in section 3.6: section=3.6
    - All formula chunks: chunk_type=formula
    """
    store = _get_chunk_store(app)

    if section:
        return store.query_by_section(section)

    if fee_item:
        return store.query_by_fee_and_type(fee_item_name=fee_item, chunk_type=chunk_type)

    if chunk_type and not fee_item:
        rows = store.conn.execute(
            "SELECT * FROM hierarchical_chunks WHERE chunk_type = ? ORDER BY section, source_page",
            (chunk_type,),
        ).fetchall()
        return [dict(r) for r in rows]

    raise HTTPException(
        status_code=400,
        detail="Provide at least one filter: fee_item, chunk_type, or section.",
    )


@app.get(
    "/api/v1/chunks/search",
    summary="Full-text search across chunk content",
)
async def search_chunks(
    q: str = Query(..., description="Search query e.g. 'outside ordinary hours surcharge'"),
    limit: int = Query(20, ge=1, le=100),
) -> List[dict]:
    """
    FTS5 full-text search across all verbatim chunk content.
    Useful when the exact fee item name is unknown or when looking for
    specific clause text across sections.
    """
    return _get_chunk_store(app).full_text_search(query=q, limit=limit)


# ─── Stage 2: Execution (§2.3) ────────────────────────────────────────────────

class CalculateRequest(BaseModel):
    vessel: VesselInput
    tariff_version: Optional[str] = None

    model_config = {
        "json_schema_extra": {
            "example": {
                "vessel": {
                    "port": "Durban",
                    "gross_tonnage": 51300,
                    "vessel_type": "Bulk Carrier",
                    "voyage_type": "inbound",
                    "after_hours": True,
                    "public_holiday": False,
                    "in_ballast": False,
                    "special_conditions": [],
                },
                "tariff_version": "FY2025-26",
            }
        }
    }


@app.post(
    "/api/v2/calculate",
    response_model=TariffInvoice,
    summary="Compute a full tariff invoice for a vessel (§2.3)",
)
async def calculate_tariff(request: CalculateRequest) -> TariffInvoice:
    """
    Full Stage-2 pipeline: retriever → deterministic calculator → LLM audit.

    **Steps (§2.3.1 + §2.3.2):**
    1. Retrieves all candidate fees for the vessel's port and GT from tariff_store.db,
       including fees filed under generic port labels (Multiple Ports, All Ports, etc.)
    2. Fetches supplementary exception/condition chunk context from hierarchical_store.db
    3. LLM applicability evaluation — which fees apply and which surcharges are active
    4. Deterministic formula evaluation per fee (Python eval, LLM not involved in arithmetic)
    5. LLM audit pass — validates amounts, checks exceptions, flags items for human review
    6. Returns a structured TariffInvoice with line items and subtotal

    **tariff_version** — optional version tag (e.g. `"FY2025-26"`).  Omit to use the
    active version set via `POST /api/v1/versions/activate`.

    This call involves two LLM round-trips and may take 30–90 seconds depending on
    the number of applicable fees and current API quota.
    """
    cfg = TariffPipelineConfig(tariff_version=request.tariff_version)

    # If a specific version is requested that differs from the running store,
    # open a dedicated store for this request only.
    if request.tariff_version and request.tariff_version != app.state.active_version:
        req_db = cfg.resolved_db_path
        if not req_db.exists():
            raise HTTPException(
                status_code=404,
                detail=f"Tariff DB for version '{request.tariff_version}' not found at {req_db}",
            )
        req_store = TariffStore(req_db)
        try:
            return await asyncio.to_thread(
                run_pipeline, request.vessel, req_store, app.state.chunk_store, cfg
            )
        finally:
            req_store.close()

    return await asyncio.to_thread(
        run_pipeline, request.vessel, app.state.tariff_store, app.state.chunk_store, cfg
    )
