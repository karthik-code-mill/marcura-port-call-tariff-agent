"""
Calculation Guardrail — post-formula-evaluation value enforcement.

Applied after deterministic formula evaluation (_compute_line) and before
invoice assembly.  Violations are recorded as warnings rather than hard
errors so the caller can flag items for human review instead of aborting
the entire invoice.

Checks:
  a) No negative amounts  — base, surcharge, or total must be >= 0
  b) No extreme amounts   — a single line item exceeding CALC_MAX_LINE_AMOUNT
                            almost certainly indicates a formula or GT-input error

Thresholds:
  CALC_MAX_LINE_AMOUNT — env var (default 10 000 000 ZAR per line)
                         Tune per country/currency once historical distributions
                         are available.

TODO(production-guardrails): Add per-fee-item expected-range config sourced
  from a fee benchmark dataset so thresholds are fee-specific rather than
  a single global ceiling.
"""

import logging
import os
from dataclasses import dataclass, field
from typing import List, Tuple

log = logging.getLogger(__name__)

# TODO(production-guardrails): Make this configurable per port / fee category.
CALC_MAX_LINE_AMOUNT: float = float(os.getenv("CALC_MAX_LINE_AMOUNT", "10000000"))


@dataclass
class CalcViolation:
    section: str
    fee_item: str
    amount_field: str
    value: float
    message: str


@dataclass
class CalculationGuardrailResult:
    """
    Result of a calculation guardrail check.

    violations — list of CalcViolation; empty means all clear.
    flagged_items — set of (section, fee_item) tuples that should be marked
                    for human review in the assembled invoice.
    """
    passed: bool
    violations: List[CalcViolation] = field(default_factory=list)
    flagged_items: List[Tuple[str, str]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.passed


class CalculationGuardrail:
    """
    Validate computed line items before invoice assembly.

    Returns a result describing all violations; does NOT raise — callers
    should iterate violations and add affected items to human_review_items.
    """

    def __init__(self, max_line_amount: float = CALC_MAX_LINE_AMOUNT) -> None:
        self.max_line_amount = max_line_amount

    def check(self, computed_lines: List) -> CalculationGuardrailResult:
        """
        Inspect all computed line items for negative or extreme values.

        Args:
            computed_lines: list of ComputedLineItem instances from calculator_agent.

        Returns:
            CalculationGuardrailResult — passed=True when no violations found.
        """
        violations: List[CalcViolation] = []
        flagged: List[Tuple[str, str]] = []

        for line in computed_lines:
            item_violations: List[CalcViolation] = []

            # a) Negative-amount checks
            for amount_field, amount_val in [
                ("base_amount",      line.base_amount),
                ("surcharge_amount", line.surcharge_amount),
                ("total_amount",     line.total_amount),
            ]:
                if amount_val < 0:
                    item_violations.append(CalcViolation(
                        section=line.section,
                        fee_item=line.tariff_fee_item,
                        amount_field=amount_field,
                        value=amount_val,
                        message=(
                            f"Negative {amount_field} {amount_val:.2f} — "
                            "formula sign error or inverted base-fee extracted"
                        ),
                    ))

            # b) Extreme-value check (total only to avoid double-reporting)
            if line.total_amount > self.max_line_amount:
                item_violations.append(CalcViolation(
                    section=line.section,
                    fee_item=line.tariff_fee_item,
                    amount_field="total_amount",
                    value=line.total_amount,
                    message=(
                        f"Total {line.total_amount:,.2f} exceeds ceiling "
                        f"{self.max_line_amount:,.0f} — "
                        "possible GT scale error or formula runaway"
                    ),
                ))

            if item_violations:
                violations.extend(item_violations)
                flagged.append((line.section, line.tariff_fee_item))

        passed = not violations

        if violations:
            for v in violations:
                log.warning(
                    f"[CalculationGuardrail] VIOLATION  "
                    f"section={v.section}  item={v.fee_item}  "
                    f"field={v.amount_field}  value={v.value:.2f}  "
                    f"msg={v.message}"
                )
            log.warning(
                f"[CalculationGuardrail] {len(violations)} violations across "
                f"{len(flagged)} line item(s) — flagged for human review"
            )
        else:
            log.debug(
                f"[CalculationGuardrail] All {len(computed_lines)} computed lines passed"
            )

        return CalculationGuardrailResult(
            passed=passed,
            violations=violations,
            flagged_items=flagged,
        )
