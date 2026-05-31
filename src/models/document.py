"""
Document structure models for the document preparation pipeline.
MarkdownSection is the core parse tree node shared by parser, extractor, and validation agents.
ExtractionSummary and ValidationSummary carry pipeline results through the LangGraph state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Literal, Optional

from pydantic import BaseModel


# ─── Markdown parse tree ──────────────────────────────────────────────────────

@dataclass
class MarkdownSection:
    """One node in the parsed ATX-heading tree of a Markdown document."""
    level:        int
    number:       str            # section number e.g. "3.1", or "" if unnumbered
    title:        str            # heading text without the number prefix
    heading_line: str            # original ATX line e.g. "## 3.1 PILOTAGE"
    direct_lines: List[str]      # lines before the first child heading
    children:     List["MarkdownSection"] = field(default_factory=list)

    @property
    def section_id(self) -> str:
        return self.number or self.title[:40]

    @property
    def direct_content(self) -> str:
        return "\n".join(self.direct_lines).strip()

    @property
    def full_content(self) -> str:
        parts = [self.heading_line] + self.direct_lines
        for child in self.children:
            parts.append(child.full_content)
        return "\n".join(parts)


# ─── Pipeline result models ───────────────────────────────────────────────────

class ExtractionSummary(BaseModel):
    """Output of rule_extractor_agent.run() — carried in DocumentPrepState."""
    doc_id:            str
    total_sections:    int
    done:              int
    errors:            int
    fee_items_written: int
    skipped:           int = 0


class ValidationVerdict(BaseModel):
    """Per-fee-item verdict from validation_agent."""
    section:        str
    tariff_fee_item: str
    verdict:        Literal["valid", "on_hold"]
    reason:         str


class ValidationSummary(BaseModel):
    """Output of validation_agent.run() — carried in DocumentPrepState."""
    doc_id:        str
    total_checked: int
    passed:        int
    on_hold_count: int
    on_hold_items: List[ValidationVerdict] = []
