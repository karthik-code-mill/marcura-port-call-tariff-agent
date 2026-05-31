"""
Retrieval Guardrail — confidence threshold filter.

Candidates with extraction_confidence < MIN_CONFIDENCE are rejected before
being passed to the LLM applicability evaluator.  This prevents the LLM from
reasoning about fee records whose extraction was too uncertain to be trusted.

Threshold: 0.2  (configurable via env var RETRIEVAL_MIN_CONFIDENCE)
"""

import logging
import os
from typing import List, Tuple

log = logging.getLogger(__name__)

MIN_CONFIDENCE: float = float(os.getenv("RETRIEVAL_MIN_CONFIDENCE", "0.2"))


class RetrievalGuardrail:
    """Filter candidate fee rows by extraction confidence before LLM evaluation."""

    def __init__(self, min_confidence: float = MIN_CONFIDENCE) -> None:
        self.min_confidence = min_confidence

    def filter(self, candidates: List[dict]) -> Tuple[List[dict], List[dict]]:
        """
        Partition candidates into (passed, rejected).

        Rejected candidates are logged as warnings so operators can track
        which fee records are systematically below the confidence floor.
        Returns both lists so callers can include rejected counts in telemetry.
        """
        passed:   List[dict] = []
        rejected: List[dict] = []

        for c in candidates:
            conf = float(c.get("extraction_confidence") or 0.0)
            if conf >= self.min_confidence:
                passed.append(c)
            else:
                rejected.append(c)
                log.warning(
                    f"[RetrievalGuardrail] REJECTED  "
                    f"section={c.get('section')}  "
                    f"item={c.get('tariff_fee_item')}  "
                    f"confidence={conf:.2f} < {self.min_confidence}"
                )

        if rejected:
            log.info(
                f"[RetrievalGuardrail] {len(passed)} passed, "
                f"{len(rejected)} rejected (confidence < {self.min_confidence})"
            )

        return passed, rejected
