import hmac

from fastapi import Depends, Header, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.limits import admin_limit


def check_admin_token(x_admin_token: str | None):
    token = get_settings().admin_token
    if not token:
        raise HTTPException(403, "Administrative ingestion is disabled")
    if not x_admin_token or not hmac.compare_digest(x_admin_token.encode(), token.encode()):
        raise HTTPException(401, "Invalid administrative credentials")


def require_admin(
    request: Request,
    response: Response,
    x_admin_token: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    check_admin_token(x_admin_token)
    admin_limit(request, db)
