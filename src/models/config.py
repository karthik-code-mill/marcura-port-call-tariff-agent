from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TariffPipelineConfig(BaseModel):
    tariff_version: Optional[str] = Field(
        default=None,
        description=(
            "Version tag identifying the tariff DB to query, e.g. 'FY2025-26'. "
            "Maps to tariff_store_<tag>.db in the structured store directory. "
            "None means: use the active version recorded in pipeline_config.json."
        ),
    )

    @property
    def resolved_db_path(self) -> Path:
        from document_preparation_agent import DB_PATH, db_path_for_version, load_active_version
        tag = self.tariff_version or load_active_version()
        if tag:
            return db_path_for_version(tag)
        return DB_PATH


def load_pipeline_config() -> TariffPipelineConfig:
    from document_preparation_agent import load_active_version
    return TariffPipelineConfig(tariff_version=load_active_version())
