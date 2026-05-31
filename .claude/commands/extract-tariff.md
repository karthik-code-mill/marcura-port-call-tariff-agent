You are running the **Tariff Extraction Pipeline in Claude mode**.
You replace the Gemini LLM from `document_preparation_agent.py`.
Follow every step below precisely. Do not skip batches, do not summarise fee content, do not invent values.

---

## Arguments

`$ARGUMENTS`

Parse from the arguments above:
- `<filename>` — first positional argument, the PDF filename (e.g. `Publisher-Tariff-Book-FY-2025-26.pdf`)
- `--page-start N` — first page to process, 1-indexed (default: `1`)
- `--page-end N` — last page to process, 1-indexed inclusive (default: last page)
- `--two-column` — flag; set when each PDF page is a two-column scan (default: off)
- `--version-tag TAG` — DB version label e.g. `FY2025-26` (default: auto-derived from filename)

Define these variables for use in all commands below:
- `PROJ` = `e:\app\code\ai\agentic-ai\marcura-port-call-tariff-agent`
- `HELPER` = `$PROJ\.claude\scripts\tariff_helper.py`
- `TEMP` = `$PROJ\.claude\temp`

---

## Step 1 — Read Batch Plan

Run the following command and parse the JSON output:

```
cd $PROJ && python $HELPER read-batches --pdf <filename> --page-start <N> [--page-end <N>] [--two-column] [--version-tag <TAG>]
```

From the JSON output, record:
- `doc_id` — document identifier
- `tariff_year` — fiscal year extracted from the filename
- `db_path` — full path to the DB file to write
- `pending_count` — number of batches still to process
- `batches` — full list of batch objects

If `pending_count == 0`, print:
> All batches already complete. Run db-status to see the stored fee items.

Then stop.

---

## Step 2 — Process Each Pending Batch

For **each batch** in `batches` where `is_done == false`, do Steps 2a through 2d in sequence.
Process batches in order from lowest `batch_start` to highest.

### 2a — Fetch Page Text

Run:
```
cd $PROJ && python $HELPER get-batch --pdf <filename> --batch-start <batch_start> --batch-end <batch_end> --page-start <N> [--page-end <N>] [--two-column]
```

From the JSON output, record:
- `page_contexts` — array of formatted page strings to extract from
- `first_page`, `last_page` — for progress reporting

Join `page_contexts` with `\n\n` to form the full page content for extraction.

### 2b — Extract Fee Items

Apply the **Extraction Rules** and **JSON Schema** below to the page content.
Produce a single JSON object matching the `PortTariffPayload` schema.

**Do not output any text other than the JSON object.**
**Every field must be present. Use default values when a field is absent from the source.**

---

### EXTRACTION RULES
*(from `document_preparation_agent_sp_v1.8.md`)*

You are a maritime tariff extraction specialist. Extract every fee item from the provided tariff document pages and return structured JSON.

Rules:
1. Grounding        — Never invent values. Use 0.0 for any unstated numeric field.
2. Formulas         — Python-evaluable strings. Variables: GT, base_fee, increment, lower_bound, upper_bound.
3. Fidelity         — Copy conditions, exceptions, and notes verbatim; never paraphrase.
4. GT ranges        — Normalise every band to "gt_min-gt_max" (both bounds mandatory):
                        ">50 000" / "50 000+"       → "50001-999999999"
                        "All ranges" / no qualifier  → "0-999999999"
                        "up to 5 000" / "<5 000"     → "0-5000"
                      Emit one record per GT band. Store verbatim wording in notes if needed for audit.
5. Coverage         — Extract every fee section; do not skip minor or flat fees.
6. Page numbers     — 1-indexed page of first appearance for each fee item.
7. Notes            — Catch-all for any qualifying clause, payment term, definition, reference, or caveat
                      that does not fit conditions / surcharges / exceptions. Use descriptive keys.
                      When in doubt, use notes — never discard content.
8. Unmodeled clauses — Reserve for text that is completely unprocessable without human authority or
                       external context: port-master discretion, procedural instructions to human officials,
                       or external-regulation references. Unclear fee conditions go in notes, not here.
                       Anything here triggers mandatory human review.
9. Confidence       — 1.0=explicit · 0.8=one ambiguous field · 0.6=context-inferred · 0.4=fee/conditions unclear · 0.0–0.2=unreliable→human review.
                      Notes content does NOT lower confidence.
10. Port names      — Exactly three valid forms:
                        (a) Single normalised port name, spelled identically across all records
                            (e.g. "Durban", "Cape Town", "Richards Bay", "Saldanha", "Ngqura").
                        (b) "All" — fee applies to all ports, or no specific port is named.
                        (c) "Other" — only when the document explicitly says "other ports" /
                            "remaining ports" / "unlisted ports". Not a fallback for uncertainty.
                      Never use aggregate labels ("Multiple Ports", "All Ports", "South African Ports")
                      — map these to "All". When one rule names multiple ports, emit one record per port.
                      Port resolution order: section heading > page header > document title.
11. Fee country     — One normalised country name (e.g. "South Africa") used identically across
                      all PortTariffPayload records in this run. Never vary the spelling.
12. Pass-Through /  — For statutory levies referencing outside legislation (no numeric rate matrix):
    External Fees     (a) base_fee = 0.0, incremental_fee_per_100_gt = 0.0.
                      (b) formula = "EXTERNAL_DETERMINATION_REFERENCED_SKIP".
                      (c) Full verbatim mandate text → unmodeled_clauses (triggers human review).

---

### JSON SCHEMA

Your output must be a single valid JSON object with this structure:

```json
{
  "port": "<string — see Rule 10>",
  "country": "<string — see Rule 11>",
  "currency": "<string — e.g. 'ZAR', 'USD', 'R'>",
  "fees": [
    {
      "section": "<string — section number exactly as printed, e.g. '3.6'>",
      "tariff_fee_item": "<string — UPPERCASE official fee name, e.g. 'PILOTAGE SERVICES'>",
      "port": "<string — see Rule 10>",
      "vessel_gt_range": "<string — normalised 'gt_min-gt_max', see Rule 4>",
      "base_fee": 0.0,
      "incremental_fee_per_100_gt": 0.0,
      "formula": "<string — Python expression or '' or 'EXTERNAL_DETERMINATION_REFERENCED_SKIP'>",
      "conditions": ["<verbatim condition text>"],
      "surcharges": [
        {"condition": "<verbatim trigger condition>", "percentage": 0.0}
      ],
      "exceptions": [
        {"text": "<verbatim exception text>", "machine_handled": false, "source": {}}
      ],
      "notes": {"<key>": "<verbatim text>"},
      "unmodeled_clauses": ["<verbatim unprocessable clause>"],
      "extraction_confidence": 1.0,
      "source_page": 0
    }
  ]
}
```

**Important:**
- `fees` must be an array; emit one element per GT band per fee item.
- All numeric fields must be JSON numbers (not strings).
- `conditions`, `surcharges`, `exceptions`, `unmodeled_clauses` default to `[]`.
- `notes` defaults to `{}`.
- `formula` defaults to `""`.
- `extraction_confidence` defaults to `1.0`.

---

### 2c — Save Extraction to Temp File

Write your extracted JSON to:
```
$TEMP\batch_<batch_start>_<batch_end>.json
```

Use the **Write tool** to save it (do not use Bash echo). Ensure the file contains only valid JSON — no markdown fences, no explanatory text, just the raw JSON object.

### 2d — Write to Database

Run:
```
cd $PROJ && python $HELPER write-batch \
  --db <db_path> \
  --doc-id <doc_id> \
  --tariff-year <tariff_year> \
  --batch-start <batch_start> \
  --batch-end <batch_end> \
  --version-tag <TAG> \
  --payload-file .claude\temp\batch_<batch_start>_<batch_end>.json
```

Report: `PDF pages <first_page>–<last_page>: <items_written> fee items written (port=<port>)`

If `errors` array in the response is non-empty, log each error but continue to the next batch.

---

## Step 3 — Final Status Report

After all pending batches are processed, run:

```
cd $PROJ && python $HELPER db-status --db <db_path> --doc-id <doc_id>
```

Print a summary:
```
Pipeline complete
  Version  : <version_tag>
  DB       : <db_path>
  Fee items: <total_fee_items>
  Batches  : <done> done, <errors> with errors
```

---

## Usage Examples

```
/extract-tariff Publisher-Tariff-Book-FY-2025-26.pdf --page-start 5 --page-end 27 --two-column --version-tag FY2025-26

/extract-tariff Publisher-Tariff-Book-FY-2025-26.pdf --version-tag FY2025-26
```
