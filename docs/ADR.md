# ADR - Archietcural Decision Record

## 1 Framework
For the Execution stage the agent framework choosen is Lang graph,
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