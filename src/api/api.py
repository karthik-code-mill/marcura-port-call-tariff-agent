"""
Tariff Calculation API
======================
FastAPI entry point for the full two-stage tariff pipeline.

Stage 1 — Document Preparation (§2.2)
  POST  /api/v1/prepare              Trigger fee extraction for a PDF
  GET   /api/v1/status/{doc_id}      Ingestion status
  GET   /api/v1/fees                 Query extracted fee items (port + GT filter)
  GET   /api/v1/files                List available PDFs in the raw directory
  GET   /api/v1/versions             List all tariff DB versions
  POST  /api/v1/versions/activate    Switch the active tariff version

Stage 2 — Execution (§2.3)
  POST  /api/v2/calculate            Compute a full tariff invoice for a vessel

Run:
    uvicorn api.api:app --reload --port 8000 --app-dir src
"""

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from pydantic import BaseModel

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.document_preparation_agent import (
    RAW_DIR,
    TariffStore,
    db_path_for_version,
    list_available_versions,
    load_active_version,
    run_pass1,
    run_pass2,
    save_active_version,
    _doc_id,
)
from agents.hierarchical_chunk_agent import CHUNK_DB_PATH, ChunkStore
from models.config import TariffPipelineConfig
from models.vessel import VesselInput
from models.invoice import TariffInvoice
from orchestrator import run_pipeline

_DEFAULT_COUNTRY = "South Africa"


# ─── App lifecycle ────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    active_tag = load_active_version(_DEFAULT_COUNTRY)
    if active_tag:
        active_db = db_path_for_version(_DEFAULT_COUNTRY, active_tag)
        app.state.tariff_store   = TariffStore(active_db)
        app.state.active_version = active_tag
        app.state.active_country = _DEFAULT_COUNTRY
    else:
        app.state.tariff_store   = None
        app.state.active_version = None
        app.state.active_country = _DEFAULT_COUNTRY
    app.state.chunk_store = ChunkStore(CHUNK_DB_PATH) if CHUNK_DB_PATH.exists() else None
    yield
    if app.state.tariff_store:
        app.state.tariff_store.close()
    if app.state.chunk_store:
        app.state.chunk_store.close()


app = FastAPI(
    title="Automated Vessel Tariff Calculation API",
    description="Stage-1 (§2.2): ingest port tariff PDFs.\nStage-2 (§2.3): compute a vessel tariff invoice.",
    version="3.0.0",
    lifespan=lifespan,
)


# ─── Request / Response Models ────────────────────────────────────────────────

class PrepareRequest(BaseModel):
    filename: str
    country:  str = _DEFAULT_COUNTRY
    version_tag: Optional[str] = None
    page_start: int = 1
    page_end: Optional[int] = None

    model_config = {"json_schema_extra": {"example": {
        "filename": "Publisher-Tariff-Book-FY-2025-26.pdf",
        "country": "South Africa",
        "version_tag": "FY2025-26-v3.2",
    }}}


class JobResponse(BaseModel):
    doc_id: str
    filename: str
    status: str
    message: str


class ActivateVersionRequest(BaseModel):
    country: str = _DEFAULT_COUNTRY
    version_tag: str

    model_config = {"json_schema_extra": {"example": {"country": "South Africa", "version_tag": "FY2025-26-v3.2"}}}


class CalculateRequest(BaseModel):
    vessel: VesselInput
    tariff_version: Optional[str] = None

    model_config = {"json_schema_extra": {"example": {
        "vessel": {
            "country": "South Africa",
            "port": "Durban",
            "gross_tonnage": 51300,
            "vessel_type": "Bulk Carrier",
            "voyage_type": "inbound",
            "cargo_type": "Iron Ore",
        },
        "tariff_version": "FY2025-26-v3.2",
    }}}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _resolve_pdf(filename: str) -> Path:
    pdf_path = RAW_DIR / filename
    if not pdf_path.exists():
        available = [f.name for f in sorted(RAW_DIR.glob("*.pdf"))]
        raise HTTPException(status_code=404, detail={"error": f"'{filename}' not found", "available": available})
    return pdf_path


def _require_tariff_store(app):
    if app.state.tariff_store is None:
        raise HTTPException(status_code=503, detail="No tariff store loaded. POST /api/v1/versions/activate first.")
    return app.state.tariff_store


# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def root():
    return {"service": "Tariff API v3", "docs": "/docs"}


@app.get("/api/v1/files", summary="List available PDF files")
async def list_files() -> List[str]:
    return [f.name for f in sorted(RAW_DIR.glob("*.pdf"))]


@app.get("/api/v1/versions", summary="List all tariff database versions")
async def get_versions(country: str = Query(_DEFAULT_COUNTRY)) -> List[dict]:
    return list_available_versions(country)


@app.post("/api/v1/versions/activate", summary="Switch the active tariff version")
async def activate_version(request: ActivateVersionRequest) -> dict:
    versions = [v["version_tag"] for v in list_available_versions(request.country)]
    if request.version_tag not in versions:
        raise HTTPException(status_code=404, detail=f"Version '{request.version_tag}' not found for {request.country}. Available: {versions}")
    save_active_version(request.country, request.version_tag)
    return {"country": request.country, "active_version": request.version_tag, "message": "Active version updated."}


@app.post("/api/v1/prepare", response_model=JobResponse, summary="Trigger §2.2 fee extraction", status_code=202)
async def trigger_prepare(request: PrepareRequest, background_tasks: BackgroundTasks) -> JobResponse:
    """Dispatch Pass 1 + Pass 2 pipeline for the given PDF in the background."""
    pdf_path    = _resolve_pdf(request.filename)
    from agents.document_preparation_agent import _tariff_year_from_name
    tariff_year = _tariff_year_from_name(pdf_path.name)
    version_tag = request.version_tag or tariff_year or pdf_path.stem
    did         = _doc_id(pdf_path, request.page_start, request.page_end)

    async def _run():
        md = run_pass1(pdf_path, page_start=request.page_start, page_end=request.page_end,
                       country=request.country, version_tag=version_tag)
        db   = db_path_for_version(request.country, version_tag)
        store = TariffStore(db)
        try:
            run_pass2(md, store, doc_id=did, tariff_year=tariff_year,
                      country_override=request.country, version_tag=version_tag)
        finally:
            store.close()

    background_tasks.add_task(_run)
    return JobResponse(doc_id=did, filename=request.filename, status="queued",
                       message=f"Extraction started for {request.country} v{version_tag}. Poll /api/v1/status/{did}.")


@app.get("/api/v1/status/{doc_id}", summary="Ingestion status")
async def get_status(doc_id: str) -> dict:
    store = _require_tariff_store(app)
    chunk_status = app.state.chunk_store.get_ingestion_status(doc_id) if app.state.chunk_store else {"status": "not_built"}
    return {"doc_id": doc_id, "fee_extraction": store.get_ingestion_status(doc_id), "chunk_extraction": chunk_status}


@app.get("/api/v1/fees", summary="Query extracted fee items")
async def query_fees(
    port: str = Query(...),
    gt: float = Query(...),
    country: str = Query(_DEFAULT_COUNTRY),
) -> List[dict]:
    store = _require_tariff_store(app)
    rows = store.conn.execute(
        "SELECT * FROM tariff_fee_items WHERE port IN (?, 'All') AND gt_min <= ? AND gt_max >= ? AND (country = ? OR country = '') ORDER BY section",
        (port, gt, gt, country),
    ).fetchall()
    return [dict(r) for r in rows]


# ─── Stage 2: Calculate ───────────────────────────────────────────────────────

@app.post("/api/v2/calculate", response_model=TariffInvoice, summary="Compute tariff invoice (§2.3)")
async def calculate_tariff(request: CalculateRequest) -> TariffInvoice:
    country = request.vessel.country
    version = request.tariff_version

    cfg    = TariffPipelineConfig(country=country, tariff_version=version)
    req_db = cfg.resolved_db_path
    if not req_db.exists():
        raise HTTPException(status_code=404, detail=f"Tariff DB not found at {req_db}. Run POST /api/v1/prepare first.")

    req_store = TariffStore(req_db)
    try:
        return await asyncio.to_thread(run_pipeline, request.vessel, req_store, app.state.chunk_store, cfg)
    finally:
        req_store.close()
