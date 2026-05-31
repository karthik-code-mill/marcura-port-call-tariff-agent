from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel, Field


class ComputedLineItem(BaseModel):
    section: str
    tariff_fee_item: str
    port: str
    vessel_gt_range: str
    base_amount: float
    surcharge_amount: float
    total_amount: float
    formula_used: str
    formula_inputs: Dict[str, Any]
    active_surcharges: List[str]
    computation_error: str = ""
    source_page: int


class AuditVerdict(BaseModel):
    section: str
    tariff_fee_item: str
    computed_amount: float
    validated: bool = Field(description="True if the amount passed all audit checks")
    audit_notes: str = Field(default="")
    flag_for_human_review: bool = Field(default=False)
    review_reason: str = Field(default="")


class AuditResponse(BaseModel):
    verdicts: List[AuditVerdict]
    overall_notes: str = Field(default="")


class TariffLineItem(BaseModel):
    section: str
    tariff_item: str
    port: str
    gt_range: str
    base_amount: float
    surcharge_amount: float
    total: float
    formula_used: str
    active_surcharges: List[str]
    notes: str
    source_page: int
    needs_human_review: bool
    review_reason: str


class TariffInvoice(BaseModel):
    port: str
    vessel_type: str
    gross_tonnage: float
    voyage_type: str
    currency: str = "ZAR"
    line_items: List[TariffLineItem]
    subtotal: float
    computation_notes: str
    human_review_items: List[str]
    retriever_skipped_fees: int
