import hmac

from fastapi import Header, HTTPException, Response

from app.config import get_settings


def require_admin(response: Response, x_admin_token: str | None = Header(default=None)):
    response.headers["Cache-Control"] = "no-store"
    token = get_settings().admin_token
    if not token:
        raise HTTPException(403, "Administrative ingestion is disabled")
    if not x_admin_token or not hmac.compare_digest(x_admin_token.encode(), token.encode()):
        raise HTTPException(401, "Invalid administrative credentials")
