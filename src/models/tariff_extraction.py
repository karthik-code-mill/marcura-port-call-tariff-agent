from __future__ import annotations

from typing import Any, Dict, List

from pydantic import BaseModel, Field


class SurchargeItem(BaseModel):
    condition:  str   = Field(description="Full trigger condition verbatim from the document.")
    percentage: float = Field(description="Percentage surcharge, e.g. 50.0 means ×1.50.")


class ExceptionItem(BaseModel):
    text:            str            = Field(description="Verbatim exception clause text.")
    machine_handled: bool           = Field(default=False)
    source:          Dict[str, Any] = Field(default_factory=dict)


class TariffFeeItem(BaseModel):
    section:      str = Field(description="Section number exactly as printed, e.g. '3.6', '4.1.2'.")
    section_name: str = Field(
        default="",
        description="Human-readable heading of the section. Copy verbatim from the section heading.",
    )
    tariff_fee_item: str = Field(description="Official fee name in UPPERCASE exactly as in the document.")
    port: str = Field(
        description=(
            "Port this fee applies to. Values: named port (e.g. 'Durban'), "
            "'All' (no port restriction), or 'Other' (explicitly other/remaining ports)."
        )
    )
    vessel_type: str = Field(
        default="All",
        description="Vessel or cargo type this fee applies to. Use 'All' when unrestricted.",
    )
    vessel_gt_range: str = Field(
        description="GT band as 'gt_min-gt_max'. Both bounds mandatory. 'All ranges' → '0-999999999'."
    )
    base_fee:                   float                = Field(default=0.0)
    incremental_fee_per_100_gt: float                = Field(default=0.0)
    formula:                    str                  = Field(default="")
    conditions:                 List[str]            = Field(default_factory=list)
    surcharges:                 List[SurchargeItem]  = Field(default_factory=list)
    exceptions:                 List[ExceptionItem]  = Field(default_factory=list)
    notes:                      Dict[str, str]       = Field(default_factory=dict)
    unmodeled_clauses:          List[str]            = Field(default_factory=list)
    extraction_confidence:      float                = Field(default=1.0)
    source_page:                int                  = Field(default=0)


class PortTariffPayload(BaseModel):
    port:     str                 = Field(description="Port name from document title/header.")
    country:  str                 = Field(default="")
    currency: str                 = Field(default="")
    fees:     List[TariffFeeItem] = Field(default_factory=list)
