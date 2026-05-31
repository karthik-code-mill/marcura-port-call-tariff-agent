from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel, Field

from .vessel import VesselInput


class FeeApplicabilityVerdict(BaseModel):
    section: str = Field(description="Section number from the fee record")
    tariff_fee_item: str = Field(description="Fee item name from the record")
    applies: bool = Field(description="True if this fee applies to the given vessel")
    reasoning: str = Field(description="Brief explanation of why it applies or not")
    active_surcharge_conditions: List[str] = Field(
        default_factory=list,
        description="Verbatim surcharge condition texts that are triggered for this vessel",
    )
    needs_human_review: bool = Field(default=False)
    review_reason: str = Field(default="")


class ApplicabilityResponse(BaseModel):
    verdicts: List[FeeApplicabilityVerdict]


class ApplicableFeeRecord(BaseModel):
    """Fee record confirmed applicable to the vessel, formula variables bound."""
    section: str
    tariff_fee_item: str
    port: str
    vessel_gt_range: str
    base_fee: float
    incremental_fee_per_100_gt: float
    formula: str
    conditions: List[str]
    surcharges: List[Dict[str, Any]]
    exceptions: List[Dict[str, Any]]
    notes: Dict[str, str]
    unmodeled_clauses: List[str]
    extraction_confidence: float
    source_page: int
    # Added by retriever
    applicability_reasoning: str
    active_surcharge_conditions: List[str]
    needs_human_review: bool
    review_reason: str
    gt_lower_bound: float
    gt_upper_bound: float


class RetrieverOutput(BaseModel):
    vessel: VesselInput
    applicable_fees: List[ApplicableFeeRecord]
    not_applicable: List[Dict[str, Any]]
    human_review_items: List[str]
