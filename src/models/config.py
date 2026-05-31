from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TariffPipelineConfig(BaseModel):
    country: str = Field(default="South Africa", description="Country whose tariff schedule to load")
    tariff_version: Optional[str] = Field(
        default=None,
        description="Version tag, e.g. 'FY2025-26-v3.2'. None = use active version from app_config.json.",
    )

    @property
    def resolved_db_path(self) -> Path:
        from agents.document_preparation_agent import db_path_for_version, load_active_version
        tag = self.tariff_version or load_active_version(self.country)
        if not tag:
            raise ValueError(f"No active version found for country '{self.country}'. Pass --version-tag explicitly.")
        return db_path_for_version(self.country, tag)


def load_pipeline_config(country: str = "South Africa") -> "TariffPipelineConfig":
    from agents.document_preparation_agent import load_active_version
    return TariffPipelineConfig(country=country, tariff_version=load_active_version(country))
