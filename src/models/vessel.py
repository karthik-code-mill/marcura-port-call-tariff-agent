from pydantic import BaseModel, Field
from typing import List


class VesselInput(BaseModel):
    country: str = Field(default="South Africa", description="Country whose tariff schedule applies, e.g. 'South Africa'")
    port: str = Field(description="Port name exactly as in tariff records, e.g. 'Durban'")
    gross_tonnage: float = Field(description="Vessel gross tonnage (GT)")
    vessel_type: str = Field(default="", description="e.g. 'Bulk Carrier', 'Tanker', 'Container Vessel'")
    voyage_type: str = Field(default="inbound", description="'inbound' (arriving) or 'outbound' (departing)")
    after_hours: bool = Field(default=False, description="Service outside ordinary working hours")
    public_holiday: bool = Field(default=False, description="Service on a public holiday")
    in_ballast: bool = Field(default=False, description="Vessel arriving/departing in ballast (no cargo)")
    cargo_type: str = Field(default="", description="Cargo commodity, e.g. 'Iron Ore', 'Coal', 'Crude Oil'")
    special_conditions: List[str] = Field(default_factory=list, description="Additional conditions, e.g. ['tug_standby_cancelled']")
