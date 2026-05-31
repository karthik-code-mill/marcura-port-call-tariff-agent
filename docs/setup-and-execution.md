# Setup & Execution Guide

This guide covers everything needed to go from a fresh checkout to a running API that produces vessel tariff invoices.

---

## Prerequisites

| Requirement | Minimum | Notes |
|-------------|---------|-------|
| Python | 3.11 | Tested on 3.11.9 |
| pip | 23+ | Comes with Python 3.11 |
| Google API key | — | Gemini 2.5 Flash (default LLM) |
| Tariff PDF | — | Required only for Stage 1 ingestion |

The system defaults to **Gemini 2.5 Flash** via the Google AI API. OpenAI and Anthropic are also supported — see [Switching LLM Provider](#switching-llm-provider).

---

## 1. Installation

```bash
# Clone and enter the project
cd marcura-port-call-tariff-agent

# Create and activate a virtual environment
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

---

## 2. Environment Configuration

Create a `.env` file in the project root:

```bash
# .env

# ── LLM Provider ──────────────────────────────────────────────────────────────
# Format:  <langchain-provider-prefix>:<model-name>
# Default: Google Gemini (used by all agents)
GOOGLE_API_KEY=your-google-api-key-here
LLM_MODEL=google_genai:gemini-2.5-flash

# ── Optional: alternate providers ─────────────────────────────────────────────
# To use OpenAI:
#   pip install langchain-openai
#   OPENAI_API_KEY=sk-...
#   LLM_MODEL=openai:gpt-4o

# To use Anthropic:
#   pip install langchain-anthropic
#   ANTHROPIC_API_KEY=sk-ant-...
#   LLM_MODEL=anthropic:claude-sonnet-4-6

# ── Guardrail thresholds (optional overrides) ──────────────────────────────────
# RETRIEVAL_MIN_CONFIDENCE=0.2     # Exclude fee records below this confidence
# CALC_MAX_LINE_AMOUNT=10000000    # Flag single line items above this ZAR amount
```

> **Google Vertex AI:** If you are using Vertex AI instead of the Google AI API, set `GOOGLE_GENAI_USE_VERTEXAI=true` and configure your GCP credentials separately.

---

## 3. Project Structure

```
marcura-port-call-tariff-agent/
├── src/
│   ├── agents/
│   │   ├── parser_agent.py            ← Stage 1 Step 1: PDF → Markdown
│   │   ├── rule_extractor_agent.py    ← Stage 1 Step 2: Markdown → SQLite
│   │   ├── validation_agent.py        ← Stage 1 Step 3: DB cross-check
│   │   ├── retriever_agent.py         ← Stage 2: fee retrieval + LLM applicability
│   │   └── calculator_agent.py        ← Stage 2: formula eval + LLM audit
│   ├── api/
│   │   └── api.py                     ← FastAPI application (entry point)
│   ├── orchestrator/
│   │   ├── document_prep_pipeline.py  ← LangGraph Stage 1 graph
│   │   └── pipeline.py                ← LangGraph Stage 2 graph
│   └── monitoring/
│       ├── telemetry.py               ← OpenTelemetry setup
│       └── business_metrics.py        ← KPI counters
├── guardrails/
│   ├── vessel_input_guardrail.py
│   ├── retrieval_guardrail.py
│   ├── calculation_guardrail.py
│   └── llm_output_guardrail.py
├── context-layer/
│   ├── config/
│   │   ├── app_config.json            ← Active version registry (auto-managed)
│   │   └── validation_holds.json      ← On-hold records (auto-managed)
│   └── rag/
│       ├── raw/                       ← DROP YOUR PDF HERE
│       ├── out/                       ← Parser output (Markdown files)
│       └── db/                        ← Tariff store SQLite databases (per version)
├── prompts/                           ← LLM system prompts (per agent)
├── requirements.txt
└── .env
```

---

## 4. Start the API Server

```bash
# From the project root
uvicorn api.api:app --reload --port 8000 --app-dir src
```

The API will be available at:
- **Swagger UI:** http://localhost:8000/docs
- **ReDoc:** http://localhost:8000/redoc
- **Root:** http://localhost:8000/

On startup the server loads the active tariff version from `context-layer/config/app_config.json`. If no active version is set (first run), it starts without a tariff store — that is fine, ingestion comes next.

---

## Workflow A — Ingest a New Tariff Document

Use this path when you have a PDF that has not been processed yet.

```
PDF → parse → extract → validate → activate → calculate
```

### Step A1 — Place the PDF

Copy the tariff PDF into the raw input directory:

```
context-layer/rag/raw/Publisher-Tariff-Book-FY-2025-26.pdf
```

Confirm it is visible:

```bash
curl http://localhost:8000/api/v1/files
```

```json
["Publisher-Tariff-Book-FY-2025-26.pdf"]
```

---

### Step A2 — Trigger Ingestion

```bash
curl -X POST http://localhost:8000/api/v1/prepare \
  -H "Content-Type: application/json" \
  -d '{
    "filename":    "Publisher-Tariff-Book-FY-2025-26.pdf",
    "country":     "South Africa",
    "version_tag": "FY2025-26-v3.2",
    "page_start":  5
  }'
```

```json
{
  "doc_id":   "abc123def456",
  "filename": "Publisher-Tariff-Book-FY-2025-26.pdf",
  "status":   "queued",
  "message":  "Pipeline queued for South Africa / FY2025-26-v3.2. Poll /api/v1/status/abc123def456."
}
```

**Save the `doc_id`** — you need it to poll progress.

> **Two-column PDF layout?** Add `"two_column_layout": true` to reflow the text correctly before extraction.

> **Re-running extraction only** (PDF already parsed): Add `"skip_parse": true` to reuse the existing Markdown file and skip the PDF conversion step. Useful after a quota error interrupted extraction mid-run.

The pipeline runs three steps in the background:
1. **Parser** — PDF → Markdown (30–90 s depending on page count)
2. **Rule Extractor** — Markdown → SQLite fee records (1 LLM call per fee section, ~5–15 min for a full tariff document)
3. **Validation** — DB cross-check on low-confidence records (1 LLM call per flagged item)

---

### Step A3 — Poll Ingestion Status

```bash
curl http://localhost:8000/api/v1/status/abc123def456
```

**While running:**
```json
{
  "doc_id": "abc123def456",
  "pipeline_job": {
    "status":  "running",
    "message": "Pipeline running…"
  },
  "fee_extraction": {
    "total_sections": 22,
    "done": 14,
    "errors": 0,
    "total_fee_items": 312
  },
  "validation_holds": { "count": 0, "items": [] }
}
```

**When complete:**
```json
{
  "doc_id": "abc123def456",
  "pipeline_job": {
    "status":  "done",
    "message": "Done — 415 fee items extracted, 3 on hold.",
    "extraction_summary": {
      "doc_id": "abc123def456",
      "total_sections": 22,
      "done": 22,
      "errors": 0,
      "fee_items_written": 415,
      "skipped": 0
    },
    "validation_summary": {
      "doc_id":        "abc123def456",
      "total_checked": 18,
      "passed":        15,
      "on_hold_count": 3
    }
  },
  "fee_extraction": {
    "total_sections": 22,
    "done": 22,
    "errors": 0,
    "total_fee_items": 415
  },
  "validation_holds": {
    "count": 3,
    "items": [
      "3.7__TANKER FIRE WATCH",
      "1.2__SAMSA LEVY",
      "3.5__VESSEL MOORING SERVICES"
    ]
  }
}
```

> **On-hold items** are excluded from invoices automatically. Their `extraction_confidence` is set to `0.0` in the DB and they are recorded in `validation_holds.json`. See [Viewing and Clearing Holds](#viewing-and-clearing-holds).

---

### Step A4 — Activate the Version

```bash
curl -X POST http://localhost:8000/api/v1/versions/activate \
  -H "Content-Type: application/json" \
  -d '{
    "country":     "South Africa",
    "version_tag": "FY2025-26-v3.2"
  }'
```

```json
{
  "country":        "South Africa",
  "active_version": "FY2025-26-v3.2",
  "message":        "Active version updated in app_config.json. Pass tariff_version explicitly in /api/v2/calculate to use it immediately."
}
```

> The server-side tariff store is loaded at startup. To use the newly activated version without restarting, pass `"tariff_version"` explicitly in the calculate request (Step A5).

---

### Step A5 — Calculate a Tariff Invoice

```bash
curl -X POST http://localhost:8000/api/v2/calculate \
  -H "Content-Type: application/json" \
  -d '{
    "vessel": {
      "country":            "South Africa",
      "port":               "Durban",
      "gross_tonnage":      51300,
      "vessel_type":        "Bulk Carrier",
      "voyage_type":        "inbound",
      "cargo_type":         "Iron Ore",
      "after_hours":        false,
      "public_holiday":     false,
      "in_ballast":         false,
      "special_conditions": []
    },
    "tariff_version": "FY2025-26-v3.2"
  }'
```

See [Invoice Response](#invoice-response) for the full response shape.

---

## Workflow B — Calculate Against an Existing Database

Use this path when the tariff database is already ingested and `app_config.json` already has an active version (the normal daily-use case).

```bash
# 1. Start the server — it auto-loads the active version
uvicorn api.api:app --reload --port 8000 --app-dir src

# 2. Verify the active version
curl http://localhost:8000/api/v1/versions?country=South+Africa

# 3. Calculate — omit tariff_version to use the active version
curl -X POST http://localhost:8000/api/v2/calculate \
  -H "Content-Type: application/json" \
  -d '{
    "vessel": {
      "country":        "South Africa",
      "port":           "Durban",
      "gross_tonnage":  51300,
      "vessel_type":    "Bulk Carrier",
      "voyage_type":    "inbound",
      "cargo_type":     "Iron Ore"
    }
  }'
```

---

## Invoice Response

A successful `/api/v2/calculate` returns a `TariffInvoice`:

```json
{
  "port":              "Durban",
  "vessel_type":       "Bulk Carrier",
  "gross_tonnage":     51300,
  "voyage_type":       "inbound",
  "currency":          "ZAR",
  "subtotal":          231031.26,
  "computation_notes": "7 fee items computed deterministically. LLM audit: all amounts within expected ranges.",
  "human_review_items": [],
  "retriever_skipped_fees": 5,
  "line_items": [
    {
      "section":          "1.1",
      "tariff_item":      "LIGHT DUES ON VESSELS",
      "port":             "All",
      "gt_range":         "All ranges",
      "base_amount":      63755.64,
      "surcharge_amount": 0.00,
      "total":            63755.64,
      "formula_used":     "GT * 1.243",
      "active_surcharges": [],
      "notes":            "",
      "source_page":      9,
      "needs_human_review": false,
      "review_reason":    ""
    },
    {
      "section":          "3.3",
      "tariff_item":      "PILOTAGE SERVICES",
      "port":             "Durban",
      "gt_range":         "All ranges",
      "base_amount":      25043.96,
      "surcharge_amount": 0.00,
      "total":            25043.96,
      "formula_used":     "19753.04 + (ceil(GT / 100) * 10.32)",
      "active_surcharges": [],
      "notes":            "Minimum fee R250.00.",
      "source_page":      13,
      "needs_human_review": false,
      "review_reason":    ""
    }
  ]
}
```

**Fields to note:**

| Field | Meaning |
|-------|---------|
| `needs_human_review` | `true` when the system cannot compute the fee with full confidence |
| `review_reason` | Why the item needs review (formula error, unmodeled clause, guardrail flag) |
| `human_review_items` | Aggregate list of fee item names flagged across the invoice |
| `retriever_skipped_fees` | Count of candidate fees the LLM determined do not apply to this vessel |
| `source_page` | Page in the original PDF where this fee was defined |

---

## Additional API Operations

### Query Extracted Fee Records

Inspect the raw fee records in the DB for a given port and vessel GT:

```bash
curl "http://localhost:8000/api/v1/fees?port=Durban&gt=51300&country=South+Africa"
```

Returns all named-port, national (`All`), and fallback (`Other`) rows in priority order.

---

### View Validation Holds

```bash
# All holds across all documents
curl http://localhost:8000/api/v1/holds

# Holds for a specific document
curl "http://localhost:8000/api/v1/holds?doc_id=abc123def456"
```

```json
{
  "doc_id": "abc123def456",
  "count":  3,
  "holds": {
    "3.7__TANKER FIRE WATCH": {
      "reason":    "Extracted base_fee of 0 does not match the rate table on source page — formula appears incomplete.",
      "timestamp": "2026-05-31T09:42:00+00:00",
      "country":   "South Africa",
      "version":   "FY2025-26-v3.2"
    }
  }
}
```

#### Clearing a Hold

On-hold items require a manual decision:

1. Review the `reason` and the source PDF page.
2. If the extraction is wrong: re-run Stage 1 with an improved prompt, or correct the DB row directly.
3. Remove the entry from `context-layer/config/validation_holds.json`.
4. Update `extraction_confidence` in the SQLite DB to a value ≥ 0.2:
   ```sql
   UPDATE tariff_fee_items
   SET extraction_confidence = 0.85
   WHERE doc_id = 'abc123def456'
     AND section = '3.7'
     AND tariff_fee_item = 'TANKER FIRE WATCH';
   ```
5. The next `/api/v2/calculate` call will include the item.

---

### Metrics Snapshot

```bash
curl http://localhost:8000/api/v1/metrics
```

```json
{
  "retrieval_hits":       12,
  "retrieval_misses":     1,
  "held_items":           3,
  "calculation_errors":   0,
  "guardrail_rejections": {
    "retrieval_confidence": 4,
    "validation_holds": 3
  },
  "token_totals": {
    "retriever_agent": 18420,
    "calculator_agent": 9340
  },
  "fee_accuracy_pct":    null,
  "fee_accuracy_samples": 0
}
```

Counters reset on server restart. For persistent metrics, see `src/monitoring/business_metrics.py`.

---

## CLI Alternatives

Both pipeline stages can be run directly from the command line without the API server.

### Stage 1 — Document Preparation (CLI)

```bash
cd src

# Full pipeline: parse PDF → extract → validate
python -m orchestrator.document_prep_pipeline \
  --country "South Africa" \
  --version-tag FY2025-26-v3.2 \
  --page-start 5

# Two-column PDF layout
python -m orchestrator.document_prep_pipeline \
  --country "South Africa" \
  --version-tag FY2025-26-v3.2 \
  --page-start 5 \
  --page-end 27 \
  --two-column-layout

# Skip parse (reuse existing Markdown after a quota error mid-extraction)
python -m orchestrator.document_prep_pipeline \
  --country "South Africa" \
  --version-tag FY2025-26-v3.2 \
  --skip-parse \
  --md-path ../context-layer/rag/out/south-africa-tariff-book-FY2025-26-v3.2.md
```

### Stage 2 — Tariff Calculation (CLI)

```bash
cd src

python -m orchestrator.pipeline \
  --country "South Africa" \
  --port "Durban" \
  --gt 51300 \
  --vessel-type "Bulk Carrier" \
  --voyage inbound \
  --cargo-type "Iron Ore" \
  --version-tag FY2025-26-v3.2

# With surcharge conditions
python -m orchestrator.pipeline \
  --port "Durban" \
  --gt 51300 \
  --vessel-type "Bulk Carrier" \
  --voyage inbound \
  --after-hours \
  --version-tag FY2025-26-v3.2
```

---

## Environment Variables Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `GOOGLE_API_KEY` | — | **Required.** Google AI API key for Gemini. |
| `LLM_MODEL` | `google_genai:gemini-2.5-flash` | LLM provider and model. See [Switching LLM Provider](#switching-llm-provider). |
| `GOOGLE_GENAI_USE_VERTEXAI` | `false` | Set `true` to use Vertex AI instead of the Google AI API. |
| `RETRIEVAL_MIN_CONFIDENCE` | `0.2` | Exclude fee records with extraction confidence below this value. |
| `CALC_MAX_LINE_AMOUNT` | `10000000` | Flag single line item totals above this ZAR amount for human review. |

---

## Switching LLM Provider

Install the relevant LangChain package and update `.env`:

**OpenAI**
```bash
pip install langchain-openai
```
```dotenv
OPENAI_API_KEY=sk-...
LLM_MODEL=openai:gpt-4o
```

**Anthropic**
```bash
pip install langchain-anthropic
```
```dotenv
ANTHROPIC_API_KEY=sk-ant-...
LLM_MODEL=anthropic:claude-sonnet-4-6
```

**Azure OpenAI**
```bash
pip install langchain-openai
```
```dotenv
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
AZURE_OPENAI_API_VERSION=2024-02-01
LLM_MODEL=azure_openai:your-deployment-name
```

The Stage 1 Validation Agent uses the Google Gemini SDK directly (`google-genai`) and reads `GOOGLE_API_KEY` regardless of `LLM_MODEL`. If you switch Stage 2 to OpenAI or Anthropic, you still need `GOOGLE_API_KEY` for Stage 1.

---

## Guardrail Behaviour

### Vessel Input Guardrail (HTTP 422)

The following inputs are rejected before any DB or LLM work is done:

| Field | Hard rejection condition |
|-------|--------------------------|
| `port` | Empty or blank |
| `gross_tonnage` | ≤ 0, or > 500,000 |
| `voyage_type` | Not `inbound` or `outbound` |

Soft warnings (logged but not rejected): GT < 100, blank `country`.

Example 422 response:
```json
{
  "error":   "vessel_input_validation_failed",
  "detail":  "Vessel input validation failed: gross_tonnage: Gross tonnage must be positive, got -100.0",
  "violations": [
    {
      "field":    "gross_tonnage",
      "message":  "Gross tonnage must be positive, got -100.0",
      "severity": "error"
    }
  ]
}
```

### Retrieval Confidence Guardrail

Records with `extraction_confidence < 0.2` are silently excluded from retrieval.
Adjust via `RETRIEVAL_MIN_CONFIDENCE` env var.

### Calculation Guardrail

Line items with negative amounts or totals > `CALC_MAX_LINE_AMOUNT` are included
in the invoice with `needs_human_review: true` rather than being dropped.

---

## Troubleshooting

**`503 No active tariff store loaded`**
The server started with no active version. Either:
- Run `POST /api/v1/prepare` first, then `POST /api/v1/versions/activate`.
- Or start the server after an active version exists in `app_config.json`.

---

**`404 Tariff DB not found`**
The `tariff_version` you specified has not been ingested yet. Check:
```bash
curl http://localhost:8000/api/v1/versions?country=South+Africa
```
Run `POST /api/v1/prepare` for the target version.

---

**Extraction job stays `running` for a long time**
The rule extractor makes one LLM call per fee section with a built-in inter-call delay to avoid rate limiting. A full 22-section South African tariff document takes approximately 10–15 minutes. Monitor server logs for `[RuleExtractorAgent]` progress lines.

---

**Quota error mid-extraction (`429`)**
The job will record the error in `pipeline_job.errors`. Re-run with `skip_parse: true`:

```bash
curl -X POST http://localhost:8000/api/v1/prepare \
  -H "Content-Type: application/json" \
  -d '{
    "filename":    "Publisher-Tariff-Book-FY-2025-26.pdf",
    "country":     "South Africa",
    "version_tag": "FY2025-26-v3.2",
    "skip_parse":  true
  }'
```

Extraction resumes from where it left off because the rule extractor checks `is_section_done()` before calling the LLM for each section.

---

**`VesselInputGuardrailError` in server logs but no 422 returned**
This happens when the exception is raised inside `asyncio.to_thread` but not propagated correctly. Check that `VesselInputGuardrailError` is imported from `guardrails.vessel_input_guardrail` at the top of `api.py` and the exception handler is registered.

---

**No fees found for a port**
```json
{ "applicable_fees": [], "not_applicable": [], "human_review_items": [] }
```
Check:
1. The port name exactly matches what is in the DB: `GET /api/v1/fees?port=Durban&gt=51300`
2. The vessel GT is within an extracted range.
3. No records were rejected by the confidence guardrail (check `/api/v1/metrics`).
4. No records are on hold for this doc: `GET /api/v1/holds?doc_id=...`
