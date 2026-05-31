"""
Vessel Input Guardrail — payload validation before retrieval.

Validates VesselInput at the pipeline boundary to reject malformed requests
before any DB query or LLM work is performed.

Design: hard violations return passed=False and must block retrieval;
soft violations (e.g. missing vessel_type) are logged as warnings so the
caller can decide whether to proceed with degraded data.
"""

import logging
from dataclasses import dataclass, field
from typing import List

log = logging.getLogger(__name__)

# Sanity GT bounds based on practical port-tariff coverage.
# Adjust per country/port authority as coverage expands.
GT_MIN: float = 100.0
GT_MAX: float = 500_000.0

VALID_VOYAGE_TYPES = {"inbound", "outbound"}


@dataclass
class VesselInputViolation:
    field: str
    message: str
    hard: bool = True  # hard=True → caller MUST block; hard=False → warning only


@dataclass
class VesselGuardrailResult:
    passed: bool
    violations: List[VesselInputViolation] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.passed

    @property
    def hard_violations(self) -> List[VesselInputViolation]:
        return [v for v in self.violations if v.hard]

    @property
    def warnings(self) -> List[VesselInputViolation]:
        return [v for v in self.violations if not v.hard]


class VesselInputGuardrailError(Exception):
    """Raised by the pipeline when vessel payload fails hard validation."""

    def __init__(self, result: VesselGuardrailResult) -> None:
        msgs = "; ".join(f"{v.field}: {v.message}" for v in result.hard_violations)
        super().__init__(f"Vessel input validation failed: {msgs}")
        self.result = result


class VesselInputGuardrail:
    """
    Validate a VesselInput before retrieval or calculation.

    Usage:
        guardrail = VesselInputGuardrail()
        result = guardrail.check(vessel)
        if not result:
            raise VesselInputGuardrailError(result)
    """

    def check(self, vessel) -> VesselGuardrailResult:
        """
        Validate vessel payload fields.

        Returns VesselGuardrailResult. If result.passed is False, the caller
        MUST NOT proceed with retrieval — it will query against an invalid key.
        """
        violations: List[VesselInputViolation] = []

        # Hard: port name is required
        if not vessel.port or not vessel.port.strip():
            violations.append(VesselInputViolation(
                field="port",
                message="Port name is required and must not be empty",
                hard=True,
            ))

        # Hard: GT must be positive
        if vessel.gross_tonnage <= 0:
            violations.append(VesselInputViolation(
                field="gross_tonnage",
                message=f"Gross tonnage must be positive, got {vessel.gross_tonnage}",
                hard=True,
            ))
        elif vessel.gross_tonnage < GT_MIN:
            # Warn: unusually small — may be a unit error (e.g. NET tonnage provided)
            violations.append(VesselInputViolation(
                field="gross_tonnage",
                message=(
                    f"Gross tonnage {vessel.gross_tonnage} is below sanity minimum {GT_MIN} — "
                    "verify that GT (not NT or DWT) was provided"
                ),
                hard=False,
            ))
        elif vessel.gross_tonnage > GT_MAX:
            # Hard: no known vessel exceeds this; likely a data entry error
            violations.append(VesselInputViolation(
                field="gross_tonnage",
                message=(
                    f"Gross tonnage {vessel.gross_tonnage} exceeds maximum threshold {GT_MAX:,.0f} — "
                    "possible data entry error"
                ),
                hard=True,
            ))

        # Hard: voyage type must be a known value
        if vessel.voyage_type not in VALID_VOYAGE_TYPES:
            violations.append(VesselInputViolation(
                field="voyage_type",
                message=(
                    f"voyage_type must be one of {VALID_VOYAGE_TYPES}, "
                    f"got '{vessel.voyage_type}'"
                ),
                hard=True,
            ))

        # Soft: country should not be blank (affects tariff DB selection)
        if not getattr(vessel, "country", None) or not vessel.country.strip():
            violations.append(VesselInputViolation(
                field="country",
                message=(
                    "Country is blank — defaulting to South Africa may retrieve "
                    "the wrong tariff schedule"
                ),
                hard=False,
            ))

        hard_failed = any(v.hard for v in violations)
        passed = not hard_failed

        for v in violations:
            level = log.warning if v.hard else log.info
            level(
                f"[VesselInputGuardrail] {'VIOLATION' if v.hard else 'WARNING'}  "
                f"field={v.field}  msg={v.message}"
            )

        if passed:
            log.debug("[VesselInputGuardrail] Vessel payload passed all checks")

        return VesselGuardrailResult(passed=passed, violations=violations)
