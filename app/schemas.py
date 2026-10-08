from datetime import timedelta
from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field, model_validator

from app.db import now


class ObservationInput(BaseModel):
    offer_id: str = Field(min_length=1, max_length=120)
    external_event_id: str = Field(min_length=1, max_length=160)
    observed_at: AwareDatetime
    variant_attributes: dict[str, str | int | float | bool]
    item_paise: int | None = Field(default=None, ge=0, le=2_000_000_000)
    shipping_paise: int | None = Field(default=None, ge=0, le=2_000_000_000)
    mrp_paise: int | None = Field(default=None, ge=0, le=2_000_000_000)
    in_stock: bool
    currency: Literal["INR"] = "INR"
    evidence_kind: Literal["recorded", "page_check", "historical_import"] = "recorded"

    @model_validator(mode="after")
    def validate_context(self):
        if self.in_stock and self.item_paise is None:
            raise ValueError("An available offer requires an item price")
        if self.observed_at > now() + timedelta(minutes=5):
            raise ValueError("Observation is in the future")
        return self


class IngestBatch(BaseModel):
    source_id: str = Field(min_length=1, max_length=80)
    observations: list[ObservationInput] = Field(min_length=1, max_length=1000)
