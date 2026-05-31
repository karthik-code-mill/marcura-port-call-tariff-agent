# Automated Vessel Tariff Calculation System — System Design

**Status:** Design / Pre-development
**Scope:** A generalizable AI agent system that ingests arbitrary port tariff documents, extracts calculation logic, and computes vessel tariffs with no hardcoded formulae, no hardcoded rates, and no manual intervention.

---

## 1. Problem Framing

The goal is an automated system that, given vessel data and a destination port, produces a fully itemized tariff invoice by *interpreting* the relevant port tariff document — not by relying on formulae or rates embedded in code.

Key characteristics of the source material and requirements:

- Tariff documents are **dense, hierarchical, condition-driven, and table-heavy**.
- The solution must **generalize** across any number of ports and documents.
- Calculation logic and rates must be **extracted from the document** at preparation time, never hardcoded.
- A match scoring **> 99%** is treated as exact (a lexical/structural threshold, not embedding distance).
- The system must operate **end-to-end without human intervention**, while safely flagging items it cannot compute.

---

## 2. Solution Design

### 2.1 Approach

The core problem is **fee computation, not retrieval**. Traditional RAG is unsuitable because tariff documents are hierarchical and tabular — flattening them into embeddings destroys structural meaning and breaks formula continuity. The solution is therefore built around two distinct phases:

1. **Document Preparation** — parse the tariff document and transform it into structured, queryable data.
2. **Execution** — at runtime, an agentic system uses the prepared data to compute applicable charges for a given vessel.

---

### 2.2 Stage 1: Document Preparation

When a port or country tariff document is ready for ingestion, it is passed to the **Tariff Extraction Agent**. This agent reads the document and converts it into structured JSON fee records — referred to as **Structured Fee Data** — enriched with port, country, and date metadata for later retrieval.

#### 2.2.1 Structured Fee Data Model

The agent will be using layout aware document parsing technique to produce the tariff fee objects. 
#Two model variants are supported:

**Model v1 — GT-range resolved**

```json
{
  "section": "3.6",
  "tariff_fee_item": "TUGS/VESSEL ASSISTANCE",
  "port": "Durban",
  "vessel_gt_range": "10001-50000",
  "base_fee": 40861.92,
  "incremental_fee_per_100_gt": 90.17,
  "formula": "...",
  "conditions": [
    "inside port",
    "standard service"
  ],
  "surcharges": [
    { "condition": "outside ordinary working hours", "percentage": 25 },
    { "condition": "additional tug requested", "percentage": 50 }
  ],
  "exceptions": [
    { "text": "<verbatim exception clause>", "machine_handled": false, "source": {} }
  ],
  "notes": { "text": "<verbatim note>" },
  "unmodeled_clauses": ["<verbatim text the extractor could not structure>"],
  "source_page": 15
}
```
<!--
**Model v2 — Formula-first**

```json
{
  "cost_item": "Light Dues",
  "formula": "...",
  "rate_table": {},
  "conditions": [],
  "exceptions": [
    { "text": "<verbatim exception clause>", "machine_handled": false, "source": {} }
  ],
  "raw_source_text": "<full original passage, unedited>",
  "extraction_confidence": 0.95,
  "unmodeled_clauses": ["<verbatim text the extractor could not structure>"]
}
```
-->
#### 2.2.2 Hierarchical Chunking

In addition to the structured fee records, the document is also indexed as **hierarchical chunks** — not flat embeddings. Each chunk preserves parent-child relationships and maps to a logical document section: intro, rules, tables, conditions, exceptions, formula notes, and references.

This index provides raw-text context to agents at query time and serves as citations for structured fee data.

```
Section
  ├── Rule paragraph
  ├── Table
  ├── Conditions
  ├── Exceptions
  └── Formula notes
```

Chunk metadata:

```json
{
  "chunk_id": "3.6.durban.gt_10001_50000",
  "parent": "3.6",
  "related_chunks": [
    "3.1.general_conditions",
    "3.6.surcharges"
  ]
}
```

Example hierarchy:

```
Section 3.6 — Tug Services
  ├── GT Fee Table
  ├── Surcharge Rules
  ├── Exceptions
  ├── Definitions
  └── Operational Notes
```

---

### 2.3 Stage 2: Execution

At runtime, the agentic system uses prepared data from Stage 1 to compute fees for a vessel arriving at a port. The entry point is an API that accepts vessel details as input. Orchestration follows a multi-agent flow.

#### 2.3.1 Tariff Fee Retriever Agent

Given vessel metadata, this agent executes the following steps:

1. **Structured Retrieval** — calls the `getApplicableFees` tool to query structured fee data by vessel type, port, and GT:

   ```sql
   WHERE
     port = 'Durban'
     AND vessel_type IN ('Bulk Carrier', 'All')
     AND gt_min <= 51300
     AND gt_max >= 51300
   ```

2. **Hierarchical Context Retrieval** — fetches supplementary raw-text context from the hierarchical chunk index (§2.2.2).

3. **Applicability Evaluation** — invokes the LLM with the candidate fee list and vessel data to verify which fees apply.

4. **Output** — returns applicable fee records with formula variables substituted. The agent does **not** execute calculations; computation is deferred to the deterministic calculator to ensure reproducibility.

**Example output:**

```json
[
  {
    "section": "2.1",
    "tariff_fee_item": "VESSEL TRAFFIC SERVICES (VTS) CHARGES",
    "port": "Durban",
    "vessel_gt_range": "All ranges",
    "base_fee": 0.0,
    "incremental_fee_per_100_gt": 0.0,
    "formula": "GT * 0.69",
    "conditions": [
      "Vessels calling the port",
      "Vessels performing port-related services within port limits and approaches"
    ],
    "surcharges": [],
    "exceptions": [
      { "text": "Vessels belonging to the SAPS and the SANDF", "machine_handled": false, "source": { "section": "2.1.1", "page": 11 } },
      { "text": "Vessels belonging to SAMSA", "machine_handled": false, "source": { "section": "2.1.1", "page": 11 } },
      { "text": "SA Medical & Research vessels", "machine_handled": false, "source": { "section": "2.1.1", "page": 11 } },
      { "text": "Vessels returning from anchorage at the order of the Harbour Master", "machine_handled": false, "source": { "section": "2.1.1", "page": 11 } },
      { "text": "Vessels resorting under Section 4, Clause 4.2 (small vessels and pleasure vessels)", "machine_handled": false, "source": { "section": "2.1.1", "page": 11 } }
    ],
    "notes": { "text": "Minimum fee R250.00. Based on Gross Tonnage per Tonnage Convention 1969." },
    "unmodeled_clauses": [],
    "source_page": 11
  },
  {
    "section": "3.3",
    "tariff_fee_item": "PILOTAGE SERVICES",
    "port": "Durban",
    "vessel_gt_range": "All ranges",
    "base_fee": 19753.04,
    "incremental_fee_per_100_gt": 10.32,
    "formula": "19753.04 + (ceiling(GT / 100) * 10.32)",
    "conditions": [
      "Normal entering or leaving the port",
      "Pilotage is compulsory"
    ],
    "surcharges": [
      { "condition": "Service terminates or commences outside ordinary working hours", "percentage": 50 },
      { "condition": "Vessel not ready 30 minutes after notified time or after pilot boards", "percentage": 50 },
      { "condition": "Request cancelled within 60 minutes prior to notified time and pilot has not boarded", "percentage": 50 }
    ],
    "exceptions": [
      { "text": "Vessels belonging to SAPS and SANDF, except when pilotage is performed on request.", "machine_handled": false, "source": { "section": "3.3", "page": 14 } }
    ],
    "notes": { "text": "Any vessel movement without Authority consent is subject to full pilotage charges." },
    "unmodeled_clauses": [
      "Pilotage dues for services other than normal entry/departure (towage, standing by, etc.) are available on application."
    ],
    "source_page": 13
  },
  {
    "section": "3.8",
    "tariff_fee_item": "BERTHING SERVICES",
    "port": "Durban",
    "vessel_gt_range": "All ranges",
    "base_fee": 2974.23,
    "incremental_fee_per_100_gt": 14.52,
    "formula": "2974.23 + (ceiling(GT / 100) * 14.52)",
    "conditions": [
      "Vessels entering or leaving a port",
      "Shifting berth, engine trials, remooring, and crewing",
      "Classified under 'Other Ports' group rate for Durban"
    ],
    "surcharges": [
      { "condition": "Service terminates or commences outside ordinary working hours", "percentage": 50 },
      { "condition": "Standby cancelled after berthing staff are on duty outside ordinary hours", "percentage": 50 },
      { "condition": "Vessel arrives or departs 30 or more minutes after notified time", "percentage": 50 }
    ],
    "exceptions": [],
    "notes": { "text": "Berthing and unberthing are charged as two separate services when a vessel shifts berth alongside." },
    "unmodeled_clauses": [
      "Fees are payable per service, including conveyance of staff."
    ],
    "source_page": 18
  },
  {
    "section": "3.9",
    "tariff_fee_item": "RUNNING OF VESSEL LINES",
    "port": "Durban",
    "vessel_gt_range": "All ranges",
    "base_fee": 1756.32,
    "incremental_fee_per_100_gt": 0.0,
    "formula": "1756.32",
    "conditions": [
      "Per service, during or outside ordinary working hours",
      "Classified under 'Other Ports' group rate for Durban"
    ],
    "surcharges": [
      { "condition": "Service terminates or commences outside ordinary working hours", "percentage": 100 }
    ],
    "exceptions": [],
    "notes": { "text": "Applies where a launch or mooring boat is used to run vessel lines from ship to bollard." },
    "unmodeled_clauses": [
      "If vessel arrives or departs 30+ minutes after notified time, charges apply per hour or part thereof (R1,756.32/hr standard; R3,512.56/hr outside hours).",
      "If a tug/vessel on standby outside ordinary hours is cancelled after standby has commenced, a fee of R1,756.32/hr applies (minimum 2 hours)."
    ],
    "source_page": 19
  }
]
```

#### 2.3.2 Tariff Fee Calculator & Response Generator

This agent receives the retriever output and orchestrates final computation and response assembly.

##### 2.3.2.1 Deterministic Calculator

A dedicated deterministic service executes each fee formula as Python code and returns a monetary result per line item. The LLM is not involved in arithmetic.

```python
pilotage_fee = base + (gt * per_gt_rate)

if after_hours:
    pilotage_fee *= 1.25
```

##### 2.3.2.2 Response Generation

The LLM audits the computed results, applies guardrails, validates totals, and renders the final structured response:

```json
[
  { "Tariff Item": "Light Dues",    "Amount (ZAR)": 60062.04  },
  { "Tariff Item": "Port Dues",     "Amount (ZAR)": 199549.22 },
  { "Tariff Item": "Towage Dues",   "Amount (ZAR)": 147074.38 },
  { "Tariff Item": "VTS Dues",      "Amount (ZAR)": 33315.75  },
  { "Tariff Item": "Pilotage Dues", "Amount (ZAR)": 47189.94  },
  { "Tariff Item": "Running Lines", "Amount (ZAR)": 19639.50  }
]
```

---

## 3. Future Enhancements

### 3.1 Knowledge Graph in Agent Memory

A lightweight domain knowledge graph held in agent memory would improve grounding and accuracy by making cross-section relationships explicit.

```
Durban
  ├── Pilotage
  │     ├── Surcharge rules
  │     └── GT formula
  ├── Tug Service
  │     ├── Tanker conditions
  │     └── After-hours surcharge
  └── Port Dues
        ├── Reductions
        └── Exemptions
```

### 3.2 Better port names handling
With IATA standard port unique names & the lookup as tool for agent as knowledge.

### 3.3 Human in the loop for document retrival failures records.
When the extraction is not on par with confidence or it has few broken data, to be flaged for human in the loop or critique pattern agent apply to ensure the retrival is 100% before Go Live
