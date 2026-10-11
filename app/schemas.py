import json
from datetime import timedelta
from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    model_validator,
)

from app.db import now


class ListedFee(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    label: str = Field(min_length=1, max_length=80)
    amount_paise: StrictInt = Field(ge=0, le=2_000_000_000)
    evidence: Literal["page_price_breakdown", "structured_offer"]


class ObservationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    offer_id: str = Field(min_length=1, max_length=120)
    external_event_id: str = Field(min_length=1, max_length=160)
    observed_at: AwareDatetime
    variant_attributes: dict[str, str | int | float | bool] = Field(min_length=1, max_length=100)
    item_paise: StrictInt | None = Field(default=None, ge=0, le=2_000_000_000)
    shipping_paise: StrictInt | None = Field(default=None, ge=0, le=2_000_000_000)
    mrp_paise: StrictInt | None = Field(default=None, ge=0, le=2_000_000_000)
    in_stock: StrictBool
    currency: Literal["INR"] = "INR"
    evidence_kind: Literal["recorded", "page_check", "historical_import"] = "recorded"
    listed_fees: list[ListedFee] | None = Field(default=None, max_length=8)

    @model_validator(mode="after")
    def validate_context(self):
        labels = [fee.label.casefold() for fee in self.listed_fees or []]
        if len(set(labels)) != len(labels):
            raise ValueError("Listed fee labels must be unique")
        if self.in_stock and self.item_paise is None:
            raise ValueError("An available offer requires an item price")
        if self.observed_at > now() + timedelta(minutes=5):
            raise ValueError("Observation is in the future")
        if len(json.dumps(self.variant_attributes, allow_nan=False)) > 10_000:
            raise ValueError("Variant attributes are too large")
        return self


class IngestBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, max_length=80)
    observations: list[ObservationInput] = Field(min_length=1, max_length=1000)
