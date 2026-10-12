"""Read recorded exact-variant offers for independently selected products."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog import offers_for, product_json
from app.db import get_db, now
from app.evidence import QUERY_BATCH_SIZE
from app.groups import member_ids
from app.limits import read_limit
from app.models import Product

router = APIRouter(prefix="/api/v1", dependencies=[Depends(read_limit)])
ProductId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_ids: list[ProductId] = Field(min_length=2, max_length=4)

    @model_validator(mode="after")
    def unique_products(self):
        if len(set(self.product_ids)) != len(self.product_ids):
            raise ValueError("Choose each product only once")
        return self


@router.post("/product-comparison")
def compare_products(body: Selection, db: Session = Depends(get_db)):
    selected = {
        product.id: product
        for product in db.scalars(
            select(Product).where(Product.id.in_(body.product_ids), Product.is_demo.is_(False))
        )
    }
    if len(selected) != len(body.product_ids):
        raise HTTPException(404, "A selected product is no longer available. Remove it and retry.")
    groups = {product_id: member_ids(db, product_id) for product_id in body.product_ids}
    members = sorted({member for group in groups.values() for member in group})
    # Batch SQL parameters, preserving every permitted offer and the user's order.
    permitted = {}
    for offset in range(0, len(members), QUERY_BATCH_SIZE):
        batch = list(
            db.scalars(
                select(Product.id).where(
                    Product.id.in_(members[offset : offset + QUERY_BATCH_SIZE]),
                    Product.is_demo.is_(False),
                )
            )
        )
        permitted.update(offers_for(db, batch))
    if any(not permitted.get(product_id) for product_id in body.product_ids):
        raise HTTPException(
            404, "A selected product has no permitted offer evidence. Remove it and retry."
        )
    seen = set()
    columns = []
    for product_id, group in groups.items():
        if seen.intersection(group):
            raise HTTPException(
                409, "Two selections belong to the same exact variant. Keep one of them."
            )
        seen.update(group)
        offers = [offer for member in group for offer in permitted.get(member, [])]
        columns.append({**product_json(selected[product_id], offers), "offers": offers})
    return {
        "checked_at": now(),
        "products": columns,
        "scope": "independent_exact_variants",
        "note": "Recorded variant details and approved exact-variant offers for each selection. Missing specifications remain unknown. This read does not fetch stores or combine products' histories.",
    }
