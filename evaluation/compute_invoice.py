"""
SUDESTADA -- Port of Durban | FY 2025-26 | Tariff Invoice Computation
======================================================================
Standalone script: reads vessel_details.json + tariff_store DB,
evaluates each applicable fee formula, and prints a line-item invoice.

ACCUMULATION LOGIC (for agent implementation):
  Step 1 - Retrieve:  SQL WHERE port IN (vessel_port, 'All', 'Other')
                           AND gt_min <= GT AND gt_max >= GT
  Step 2 - Filter:    Remove EXTERNAL_DETERMINATION_REFERENCED_SKIP,
                      duplicates, and non-applicable fee types.
  Step 3 - Evaluate:  eval(formula) with variables GT, base_fee,
                      incremental_fee_per_100_gt, increment, lower_bound,
                      upper_bound, math.
  Step 4 - Multiply:  Apply quantity multipliers (num_tugs, num_berthing
                      operations, etc.) where the fee is per-service.
  Step 5 - Accumulate: Sum all computed line amounts.
"""

import json
import math
import sqlite3
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE   = Path(__file__).resolve().parent.parent
DB     = BASE / "context-layer" / "structured" / "tariff_store_FY2025-26-v1.7.db"
VESSEL = BASE / "evaluation" / "reference" / "vessel_details.json"

# ---------------------------------------------------------------------------
# Load vessel
# ---------------------------------------------------------------------------
v           = json.loads(VESSEL.read_text())
GT          = v["technical_specs"]["gross_tonnage"]           # 51300
PORT_RAW    = v["voyage"]["destination_port"]["port_name"]    # Port of Durban
PORT        = PORT_RAW.replace("Port of ", "")                # Durban
DAYS        = v["operational_data"]["days_alongside"]          # 3.39
CARGO       = v["operational_data"]["activity"]                # Exporting Iron Ore
NUM_OPS     = v["operational_data"]["num_operations"]          # 2
VESSEL_TYPE = v["technical_specs"]["type"]                     # Bulk Carrier

# ---------------------------------------------------------------------------
# Fee selection: which fee items apply to this vessel call
#   (section, tariff_fee_item, qty, note)
#   qty = service multiplier:
#     1 = per port call (once)
#     2 = per service, entry + departure
# ---------------------------------------------------------------------------
APPLICABLE_FEES = [
    ("1.1.1", "LIGHT DUES",                               1, "Per port call"),
    ("2.1.1", "VTS CHARGES",                              1, "Per port call"),
    ("3.3",   "PILOTAGE SERVICES",                        2, "Entry + departure"),
    ("3.6",   "TUGS/VESSEL ASSISTANCE AND/OR ATTENDANCE", 2, "2 tug operations"),
    ("3.6",   "TUG/VESSEL DELAY FEE",                    1, "Per call flat fee"),
    ("3.8",   "BERTHING SERVICES",                        2, "Entry + departure"),
]

# ---------------------------------------------------------------------------
# Eval helper
# ---------------------------------------------------------------------------
def eval_formula(formula: str, record: dict, gt: float):
    """
    Evaluate a tariff formula string.
    Variables exposed:
      GT                        vessel gross tonnage
      base_fee                  fixed component from DB
      incremental_fee_per_100_gt rate per 100 GT
      increment                 alias for incremental_fee_per_100_gt
      lower_bound               gt_min (band lower bound)
      upper_bound               gt_max (band upper bound)
      math                      Python math module
    Returns float or None on skip/error.
    """
    if not formula or formula.strip() == "EXTERNAL_DETERMINATION_REFERENCED_SKIP":
        return None
    ctx = {
        "GT":                         gt,
        "base_fee":                   record["base_fee"],
        "incremental_fee_per_100_gt": record["incremental_fee_per_100_gt"],
        "increment":                  record["incremental_fee_per_100_gt"],
        "lower_bound":                record["gt_min"],
        "upper_bound":                record["gt_max"],
        "math":                       math,
        "ceil":                       math.ceil,
        "__builtins__":               {},
    }
    try:
        return float(eval(formula, ctx))
    except Exception:
        return None

# ---------------------------------------------------------------------------
# DB fetch: port-specific first, then 'All'
# ---------------------------------------------------------------------------
def fetch_record(conn, section, fee_item, port, gt):
    for port_label in [port, "All"]:
        row = conn.execute(
            """SELECT * FROM tariff_fee_items
               WHERE section=? AND tariff_fee_item=?
                 AND port=? AND gt_min <= ? AND gt_max >= ?
               LIMIT 1""",
            (section, fee_item, port_label, gt, gt),
        ).fetchone()
        if row:
            return dict(row)
    return None

# ---------------------------------------------------------------------------
# Compute
# ---------------------------------------------------------------------------
conn = sqlite3.connect(str(DB))
conn.row_factory = sqlite3.Row

grand_total = 0.0
warnings    = []
line_items  = []

for section, fee_item, qty, note in APPLICABLE_FEES:
    rec = fetch_record(conn, section, fee_item, PORT, GT)

    if rec is None:
        warnings.append(("MISSING", section, fee_item, "not found in DB"))
        continue

    formula  = rec["formula"] or ""
    if formula == "EXTERNAL_DETERMINATION_REFERENCED_SKIP":
        warnings.append(("SKIP", section, fee_item, "external/statutory"))
        continue

    unit_fee = eval_formula(formula, rec, GT)
    if unit_fee is None:
        if rec["base_fee"] > 0:
            unit_fee = rec["base_fee"]   # flat fee with no formula
        else:
            warnings.append(("WARN", section, fee_item, "formula eval failed, base_fee=0"))
            continue

    total_fee    = unit_fee * qty
    grand_total += total_fee
    line_items.append({
        "section":   section,
        "fee_item":  fee_item,
        "gt_range":  rec["vessel_gt_range"],
        "qty":       qty,
        "unit_fee":  unit_fee,
        "total_fee": total_fee,
        "page":      rec["source_page"],
        "note":      note,
        "formula":   formula,
        "base_fee":  rec["base_fee"],
        "incr":      rec["incremental_fee_per_100_gt"],
        "port_used": rec["port"],
    })

conn.close()

# ---------------------------------------------------------------------------
# Print invoice
# ---------------------------------------------------------------------------
W = 74
print()
print("=" * W)
print(f"  SUDESTADA  |  Port of Durban  |  FY 2025-26  |  GT {GT:,}")
print(f"  {VESSEL_TYPE}  |  {CARGO}  |  Days alongside: {DAYS}")
print("=" * W)
print(f"  {'SEC':<10} {'FEE ITEM':<38} {'GT RANGE':<16} {'QTY':>3}  {'UNIT FEE':>12}  {'TOTAL (ZAR)':>13}  PG")
print(f"  {'-'*10} {'-'*38} {'-'*16} {'-'*3}  {'-'*12}  {'-'*13}  --")

for li in line_items:
    label = li["fee_item"][:38]
    print(f"  {li['section']:<10} {label:<38} {li['gt_range']:<16} "
          f"{li['qty']:>3}  {li['unit_fee']:>12,.2f}  {li['total_fee']:>13,.2f}  {li['page']}")

print(f"  {'':10} {'':38} {'':16} {'':3}  {'':12}  {'-'*13}")
print(f"  {'':10} {'TOTAL COMPUTABLE FEES':38} {'':16} {'':3}  {'':12}  {grand_total:>13,.2f}")
print("=" * W)

# ---------------------------------------------------------------------------
# Warnings
# ---------------------------------------------------------------------------
if warnings:
    print()
    print("  Warnings / skipped:")
    for tag, sec, item, reason in warnings:
        print(f"  [{tag}] {sec} {item} -- {reason}")

# ---------------------------------------------------------------------------
# Derivation breakdown
# ---------------------------------------------------------------------------
print()
print("=" * W)
print("  DERIVATION DETAIL")
print("=" * W)
for li in line_items:
    print(f"\n  {li['section']}  {li['fee_item']}  (port used: {li['port_used']})")
    print(f"    GT range : {li['gt_range']}")
    print(f"    base_fee = {li['base_fee']:,.2f}   incr/100GT = {li['incr']}")
    if li["formula"]:
        print(f"    Formula  : {li['formula']}")
    print(f"    GT={GT:,}  =>  unit = {li['unit_fee']:,.2f} ZAR")
    print(f"    x {li['qty']} ({li['note']})  =>  {li['total_fee']:,.2f} ZAR")

# ---------------------------------------------------------------------------
# Accumulation logic explanation
# ---------------------------------------------------------------------------
print()
print("=" * W)
print("  ACCUMULATION LOGIC FOR YOUR AGENT")
print("=" * W)
print("""
  Step 1 - RETRIEVE (SQL per fee section):
    SELECT * FROM tariff_fee_items
    WHERE section = <section>
      AND tariff_fee_item = <fee_item>
      AND port IN (<vessel_port>, 'All')
      AND gt_min <= <GT> AND gt_max >= <GT>
    Priority: port-specific record > 'All' record > 'Other' record.

  Step 2 - FILTER:
    - Skip formula = 'EXTERNAL_DETERMINATION_REFERENCED_SKIP'
    - Skip fees the vessel is exempt from (vessel type, trade, etc.)
    - When both a port-specific and 'Other' record exist, use port-specific.

  Step 3 - EVALUATE (deterministic, no LLM):
    unit_fee = eval(record.formula, {
        'GT':                         vessel.gross_tonnage,
        'base_fee':                   record.base_fee,
        'incremental_fee_per_100_gt': record.incremental_fee_per_100_gt,
        'increment':                  record.incremental_fee_per_100_gt,
        'lower_bound':                record.gt_min,
        'upper_bound':                record.gt_max,
        'math':                       <python math module>,
    })
    If formula is empty string -> unit_fee = record.base_fee (flat rate).

  Step 4 - MULTIPLY (per-service fees):
    total_fee = unit_fee * quantity
    quantity rules:
      qty=1  per port call      Light Dues, VTS, Tug Delay Fee
      qty=2  per service x 2    Pilotage, Tugs, Berthing (entry + departure)
      qty=N  from vessel data   num_operations, num_holds, etc.

  Step 5 - ACCUMULATE:
    invoice_total = sum(item.total_fee for item in applicable_fees)
""")
