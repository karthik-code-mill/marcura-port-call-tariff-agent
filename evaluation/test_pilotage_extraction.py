"""
Pilotage extraction validation — Section 3.3
Verifies that the DB contains per-port records and computes the correct
pilotage fee for vessel SUDESTADA (GT=51300, Port of Durban).
"""

import json
import math
import sqlite3
from pathlib import Path

BASE  = Path(__file__).resolve().parent.parent
DB    = BASE / "context-layer" / "structured" / "tariff_store_FY2025-26-v1.7.db"
VESSEL = BASE / "evaluation" / "reference" / "vessel_details.json"

# ── Load vessel ───────────────────────────────────────────────────────────────
v      = json.loads(VESSEL.read_text())
GT     = v["technical_specs"]["gross_tonnage"]          # 51300
PORT   = v["voyage"]["destination_port"]["port_name"]   # Port of Durban
PORT   = PORT.replace("Port of ", "")                   # → Durban

print(f"Vessel : {v['vessel_metadata']['name']}")
print(f"GT     : {GT:,}")
print(f"Port   : {PORT}")
print()

# ── Query DB ──────────────────────────────────────────────────────────────────
conn = sqlite3.connect(str(DB))
conn.row_factory = sqlite3.Row

rows = conn.execute(
    "SELECT * FROM tariff_fee_items WHERE section='3.3' ORDER BY port"
).fetchall()

print("-" * 80)
print(f"Section 3.3 - all extracted records ({len(rows)} total)")
print("-" * 80)
print(f"  {'PORT':<22} {'BASE FEE':>12}  {'INCR/100GT':>10}  {'CONFIDENCE':>10}")
print(f"  {'-'*22} {'-'*12}  {'-'*10}  {'-'*10}")
for r in rows:
    if "PILOTAGE SERVICES" in r["tariff_fee_item"]:
        print(f"  {r['port']:<22} {r['base_fee']:>12,.2f}  {r['incremental_fee_per_100_gt']:>10.2f}  {r['extraction_confidence']:>10.1f}")
print()

# ── Find Durban record ────────────────────────────────────────────────────────
durban = conn.execute(
    "SELECT * FROM tariff_fee_items WHERE section='3.3' AND tariff_fee_item='PILOTAGE SERVICES' AND port=?",
    (PORT,)
).fetchone()

other = conn.execute(
    "SELECT * FROM tariff_fee_items WHERE section='3.3' AND tariff_fee_item='PILOTAGE SERVICES' AND port='Other'",
).fetchone()

print("-" * 80)
print(f"Extraction correctness check")
print("-" * 80)

EXPECTED_DURBAN_BASE = 19753.04
EXPECTED_DURBAN_INCR = 10.32
EXPECTED_OTHER_BASE  = 6950.12
EXPECTED_OTHER_INCR  = 11.14

def chk(label, got, expected, tol=0.01):
    ok = abs(got - expected) <= tol
    mark = "[PASS]" if ok else "[FAIL]"
    print(f"  {mark}  {label}: got {got:,.2f}  expected {expected:,.2f}")
    return ok

all_ok = True
if durban:
    all_ok &= chk("Durban base_fee          ", durban["base_fee"],                    EXPECTED_DURBAN_BASE)
    all_ok &= chk("Durban incr/100GT        ", durban["incremental_fee_per_100_gt"],  EXPECTED_DURBAN_INCR)
else:
    print("  [FAIL]  No Durban-specific record found — still collapsed to 'All'")
    all_ok = False

if other:
    all_ok &= chk("Other  base_fee          ", other["base_fee"],                     EXPECTED_OTHER_BASE)
    all_ok &= chk("Other  incr/100GT        ", other["incremental_fee_per_100_gt"],   EXPECTED_OTHER_INCR)
else:
    print("  [FAIL]  No 'Other' record found")
    all_ok = False

print()

# ── Compute fee for SUDESTADA ─────────────────────────────────────────────────
print("-" * 80)
print(f"Pilotage fee calculation for SUDESTADA  (GT={GT:,}, Port={PORT})")
print("-" * 80)

if durban:
    bf   = durban["base_fee"]
    incr = durban["incremental_fee_per_100_gt"]
    formula = durban["formula"]
    fee  = eval(formula, {"GT": GT, "base_fee": bf, "incremental_fee_per_100_gt": incr,
                          "math": math, "ceil": math.ceil, "__builtins__": {}})
    print(f"  Formula  : {formula}")
    print(f"  base_fee : {bf:>12,.2f} ZAR")
    print(f"  ceil({GT}/100) × {incr} = {math.ceil(GT/100)} × {incr} = {math.ceil(GT/100)*incr:,.2f} ZAR")
    print(f"  ─────────────────────────────")
    print(f"  TOTAL    : {fee:>12,.2f} ZAR")
    EXPECTED_FEE = 19753.04 + math.ceil(GT / 100) * 10.32
    print(f"  Expected : {EXPECTED_FEE:>12,.2f} ZAR  {'[PASS]' if abs(fee-EXPECTED_FEE)<0.01 else '[FAIL]'}")
else:
    print("  Cannot compute — Durban record missing.")

print()
print("-" * 80)
print(f"Overall : {'ALL CHECKS PASSED' if all_ok else 'ONE OR MORE CHECKS FAILED'}")
print("-" * 80)

conn.close()
