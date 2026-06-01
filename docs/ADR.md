# ADR - Archietcural Decision Record

## 0 Why this approach of RAG but Non traditional

Typically RAG would be first choice to go with for this automated tariff calculator, but the traditional rag would not work because it shoudl be a case where LLm is provided with additional snippet of context to reason better. But here the need is deterministic extraction of the tariffs from the contract and apply then autonously for an incoming vessel.

Approach was to go for Heirarchial structured meta data based RAG.
Heirarchial - because the nature of the domain tariff fee is into sections, sub sections. Context should be entire sectio or subsection area. Chunking would make incomplete context.
Structured - The data has the similiarity across the extraction. port, fee, exceptions, surcharges. But with also parts of free textual knowledges to be reasoned.
Meta Data - this will help to categorically retrive and keep the travesal deterministic & context engineered.

Could also go for page index & lllm tree RAG - This approach works when we are searching for determined context, but in this solution we have traverse and find match for all the section applicable better suited for metadata based.

Cautious design to use first extraction and then use extracted data as RAG for tariff computation for a give vessel. This way need not reason every single time of teh transaction.

### Design evolutions are

1) Extract PDF page by page with previous & next page limited cache into Structured Meta data RAG & another hierarchical data store

2) Extract PDF into MD. Then parse using MD's Heirarchial data for traversal for each section or subsection (inteligently) to LLM to extract tariffs. Extracted tariffs are coverted to heirarchial metadata based structured RAG.

3) Removed flitz used for additional heirarchial and started using Markdown's heirarachial info with parsing with inteligence.

4) Added agenic validation flow & AI Harness to control edge scenarrios

5) Tabular data extraction id impacted due to format discrepancies in the contract. Added 2 layer processing 
i) inteligent parser of the table clearing descrepancies
ii) Added injection of a parsed table form md to json Array for making LLm to be able to extract more precisely

## 1 Framework
For the implementation the agent framework choosen is Lang graph,
Other consideration of a simple python based steps executor will save langgraph overheads. But i wanted to add on HITL for future roadmap.
It might look overkill for the flows specially data prep but as the  project evolves from assignment to full context this could be more appropirate(open for modifications). But defintely good to have a reassessment.

Other considerations - simple python based steps ( Not choosen)
# orchestrator/document_prep_pipeline.py — no LangGraph needed

def run_document_prep_pipeline(pdf_path, country, version_tag, ...) -> DocPrepResult:
    result = DocPrepResult()

    # Step 1: Parse
    try:
        result.md_path = parser_agent.run(pdf_path, ...)
        result.step_log.append("parse: done")
    except ParserError as e:
        result.errors.append(f"parse: {e}")
        return result          # can't continue without MD

    # Step 2: Extract
    try:
        result.extraction_summary = rule_extractor_agent.run(result.md_path, ...)
        result.step_log.append(f"extract: {result.extraction_summary.fee_items_written} fees")
    except ExtractionError as e:
        result.errors.append(f"extract: {e}")
        return result          # can't validate without extracted data

    # Step 3: Validate
    try:
        result.validation_summary = validation_agent.run(result.md_path, ...)
        result.step_log.append(f"validate: {result.validation_summary.on_hold_count} on_hold")
    except Exception as e:
        result.errors.append(f"validate: {e}")  # validation failure is non-fatal

    return result


Flow is |
Agent 1: Tariff Retriever
    ↓ (structured fees + conditions + metadata)
Agent 2: Calculator & Response Generator
    ↓
Final output (fees + explanation + audit trail)

need frameworks that support:
explicit orchestration
structured state
tool calling
branching (optional later)
auditability

### LangGraph is built exactly for this.
Why LangGraph is ideal here:

Because your system is:

✔ deterministic
✔ state-driven
✔ multi-step
✔ needs strict handoff between agents
✔ needs debugging + traceability



### CrewAI
Use only if:
you want role-play agents (“retriever agent”, “calculator agent”)

But for your case:
❌ not ideal because:
too LLM-centric
not deterministic enough
weaker structured state handling
harder to enforce strict computation flow

## 2 Structured Fee Data

The data store - tariff_store.db is chosen to be sqlite3 for JSONB storage & retrieval with metadata filters.
The DB will have 2 tables
Table	Purpose
tariff_fee_items	397 structured fee records extracted from the SA Ports tariff PDF — the "Structured Fee Data" from §2.2.1
ingestion_log	Tracks which document batches completed. Two docs ingested (ec8e0f5decee, 55566d0ae4d5); batches 14–22 of the first doc failed with Gemini 429 quota errors
The tariff_fee_items columns map directly to the JSON model in §2.2.1: section, tariff_fee_item, vessel_gt_range, gt_min, gt_max, base_fee, incremental_fee_per_100_gt, formula, conditions, surcharges, exceptions, notes, unmodeled_clauses, source_page, extraction_confidence.

Example response results for SUDESTADA (GT=51300)

SEC     FEE ITEM                                      GT RANGE               CALC FEE (ZAR)
────────────────────────────────────────────────────────────────────────────────────────────
1.1     LIGHT DUES ON VESSELS                         All ranges               63,755.64
1.2     SAMSA LEVY                                    All ranges                    0.00  ⚠ formula missing
2.1     VTS CHARGES ON VESSELS                        All ranges               35,397.00
3.2     MARINE SERVICES INCENTIVE                     All ranges                    0.00  (discount)
3.3     PILOTAGE SERVICES                             All ranges               12,664.94  (per movement)
3.3     PILOTAGE SERVICES - PLO DUTIES                All ranges                  940.70
3.6     TUGS/VESSEL ASSISTANCE                        50 001 to 100 000        96,263.12  ✓ GT-banded match
3.6     TUGS - DELAYED ARRIVAL/DEPARTURE              All ranges               10,776.55  (if applicable)
3.8     BERTHING SERVICES                             All ranges               10,422.99
3.9     RUNNING OF VESSEL LINES                       All ranges                1,756.32
3.7     TANKER FIRE WATCH                             All ranges              ⚠ BAD CALC  (not applicable — Bulk Carrier)

## Execution result of the @document_preparation_agent
 Pipeline complete — version=FY2025-26-v1.7  415 fee items stored,


## 3 Data Store — SQLite (Assignment) → PostgreSQL (Production)

**Decision:** Use SQLite for all persistent stores (`TariffStore`, `ChunkStore`) throughout this assignment.

**Status:** Accepted for assignment scope. PostgreSQL path documented and descoped.

### Why SQLite here

SQLite requires no server, no credentials, no schema migration tooling, and no infrastructure setup. Each tariff database is a single file with a versioned name (`south-africa-tariff-store-FY2025-26-v3.3.db`). Switching versions, rolling back, or inspecting data is a file operation. For an assignment targeting a single-country dataset updated once a year, this is entirely sufficient.

The abstraction is also already in place. `TariffStore` and `ChunkStore` are the only classes in the codebase that hold a database connection. All agent code calls methods on these classes — no agent imports `sqlite3` directly. Replacing the underlying connection requires changes in exactly two files and nowhere else.

### Why not PostgreSQL for the assignment

Standing up a PostgreSQL instance (local or cloud), managing connection strings, writing migration scripts, and handling schema versioning adds infrastructure overhead with no benefit at this scale. It would shift the submission's focus from the AI pipeline to infrastructure plumbing.

### What PostgreSQL enables that SQLite cannot

**Concurrent Stage 1 ingestion.** SQLite uses file-level write locking. Two ingestion runs against the same database file at the same time — South Africa and UAE being ingested simultaneously, or two workers processing different sections of the same document in parallel — will produce `database is locked` errors. PostgreSQL uses row-level locking and handles concurrent upserts to `tariff_fee_items` correctly without any application-level coordination.

**Multi-instance API serving.** The FastAPI server is currently stateless per request, but `validation_holds.json` is a file on the local filesystem. If the API is scaled to multiple pods (Kubernetes, cloud run, multiple workers), each pod sees its own copy of the holds file. A hold written by the validation agent during ingestion on pod A is invisible to pod B when it handles a calculation request. PostgreSQL (or Redis) as the holds store eliminates this split-brain condition.

**Connection pooling.** SQLite connections are not thread-safe across processes. PostgreSQL with `pgBouncer` or SQLAlchemy's pool supports high-concurrency Stage 2 workloads without contention.

### Migration path (when needed)

The scope of changes is limited to `TariffStore.__init__` and `ChunkStore.__init__`:

- Replace `sqlite3.connect(db_path)` with a PostgreSQL driver connection (`psycopg2`, `asyncpg`, or SQLAlchemy engine)
- Convert the `CREATE TABLE` DDL: `TEXT` JSON columns → `JSONB`, `AUTOINCREMENT` → `SERIAL`, `ON CONFLICT DO UPDATE` syntax is compatible with minor adjustments
- Replace `sqlite3.Row` row access with `RealDictCursor` or equivalent dict-like rows
- Move DB routing from file paths (`db_path_for_version`) to a `DATABASE_URL` environment variable with a schema or table-prefix convention per country and version

No agent code (`retriever_agent`, `calculator_agent`, `rule_extractor_agent`, `validation_agent`) changes.

`validation_holds.json` requires a separate migration to a shared store (a `validation_holds` PostgreSQL table or Redis hash) before multi-instance deployment.

---

## Considered
Heirarchial paged Index
the problem is that most tariff PDFs are not authored like books:

TOC may be missing entirely
headings are visually obvious but not semantically tagged
tables break section detection
font sizes are inconsistent
OCR PDFs have no structure
nested numbering is often the only hierarchy signal

Option : LlamaParse (Best Quality)