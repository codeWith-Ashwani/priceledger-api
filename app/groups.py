from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import ProductMatch


def member_ids(db: Session, product_id: str) -> list[str]:
    members = {product_id}
    for _ in range(10):
        rows = db.execute(
            select(ProductMatch.left_id, ProductMatch.right_id).where(
                or_(ProductMatch.left_id.in_(members), ProductMatch.right_id.in_(members))
            )
        )
        found = {value for row in rows for value in row}
        if found.issubset(members):
            return sorted(members)
        members.update(found)
    raise HTTPException(409, "This comparison group needs identity review.")
