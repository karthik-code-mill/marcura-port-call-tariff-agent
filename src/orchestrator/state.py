"""TariffPipelineState — LangGraph state contract for the Stage-2 pipeline."""

import operator
import sys
from pathlib import Path
from typing import Annotated, List, Optional

from typing_extensions import TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models.config import TariffPipelineConfig
from models.invoice import TariffInvoice
from models.retrieval import ApplicableFeeRecord
from models.vessel import VesselInput


class TariffPipelineState(TypedDict):
    # ── input ────────────────────────────────────────────────────────────────
    vessel: VesselInput
    config: Optional[TariffPipelineConfig]

    # ── retriever output ──────────────────────────────────────────────────────
    applicable_fees:    List[ApplicableFeeRecord]
    not_applicable:     List[dict]
    human_review_items: List[str]

    # ── final output ──────────────────────────────────────────────────────────
    invoice: Optional[TariffInvoice]

    # ── diagnostics (append-only — LangGraph reducer) ────────────────────────
    errors:   Annotated[List[str], operator.add]
    step_log: Annotated[List[str], operator.add]
