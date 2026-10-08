"""Bound inbound bodies before parsing and keep errors/logs free of request evidence."""

import asyncio
import json
import logging
from time import perf_counter
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.auth import check_admin_token
from app.config import get_settings

logger = logging.getLogger("priceledger.http")
logger.setLevel(logging.INFO)
if not logger.handlers:
    logger.addHandler(logging.StreamHandler())


def strict_request_json(body: bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("Non-finite JSON number")

    parsed = json.loads(body.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    pending = [parsed]
    while pending:
        value = pending.pop()
        if isinstance(value, str):
            value.encode("utf-8")  # Reject escaped, unpaired Unicode surrogates too.
        elif isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)


class RequestSafetyMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = str(uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        started, status, response_started = perf_counter(), 500, False

        async def safe_send(message):
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status = message["status"]
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "no-referrer"
                headers["Cache-Control"] = "no-store"
            await send(message)

        try:
            headers = Headers(scope=scope)
            path = scope["path"]
            if path.rstrip("/") == "/api/v1/ingest" or path.startswith("/api/v1/admin/"):
                # Authenticate before reading potentially large private files, including
                # malformed JSON. The dependency rechecks credentials and reserves quota.
                check_admin_token(headers.get("x-admin-token"))
            if scope["method"] in {"POST", "PUT", "PATCH"}:
                settings = get_settings()
                if headers.get("content-encoding", "identity").lower() != "identity":
                    raise HTTPException(415, "Compressed request bodies are not supported")
                lengths = headers.getlist("content-length")
                if lengths:
                    if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
                        raise HTTPException(400, "Invalid Content-Length")
                    # Bound the digit count before int conversion as well as the byte size.
                    if len(lengths[0]) > 10 or int(lengths[0]) > settings.request_body_bytes:
                        raise HTTPException(413, "Request body is too large")
                body = bytearray()
                try:
                    async with asyncio.timeout(settings.request_body_timeout_seconds):
                        while True:
                            message = await receive()
                            if message["type"] == "http.disconnect":
                                status = 499
                                return
                            chunk = message.get("body", b"")
                            if len(body) + len(chunk) > settings.request_body_bytes:
                                raise HTTPException(413, "Request body is too large")
                            body.extend(chunk)
                            if not message.get("more_body", False):
                                break
                except TimeoutError as exc:
                    raise HTTPException(408, "Request body timed out") from exc
                if lengths and int(lengths[0]) != len(body):
                    raise HTTPException(400, "Content-Length does not match the body")
                if body:
                    media_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if media_type != "application/json":
                        raise HTTPException(415, "Use application/json for request bodies")
                    try:
                        strict_request_json(bytes(body))
                    except (ValueError, RecursionError) as exc:
                        raise HTTPException(
                            422, "Invalid JSON: use unique fields and finite numbers"
                        ) from exc

                delivered = False

                original_receive = receive

                async def bounded_receive():
                    nonlocal delivered
                    if not delivered:
                        delivered = True
                        return {"type": "http.request", "body": bytes(body), "more_body": False}
                    return await original_receive()

                receive = bounded_receive
            await self.app(scope, receive, safe_send)
        except HTTPException as exc:
            if response_started:
                raise
            await JSONResponse(
                {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
            )(scope, receive, safe_send)
        except Exception as exc:
            if response_started:
                raise
            # SQL statements, bound values, credentials and URLs must not be echoed/logged.
            logger.error("request_failed id=%s class=%s", request_id, type(exc).__name__)
            if isinstance(exc, IntegrityError):
                code, detail = 409, "Concurrent data conflict. Retry the request."
            elif isinstance(exc, SQLAlchemyError):
                code, detail = 503, "Database temporarily unavailable. Try again shortly."
            else:
                code, detail = (
                    500,
                    "Request failed. Use the request ID when reporting this problem.",
                )
            await JSONResponse(
                {"detail": detail},
                status_code=code,
                headers={"Retry-After": "5"} if code == 503 else None,
            )(scope, receive, safe_send)
        finally:
            route = getattr(scope.get("route"), "path", "unmatched")
            logger.info(
                "request id=%s method=%s route=%s status=%s duration_ms=%.1f",
                request_id,
                scope["method"],
                route,
                status,
                (perf_counter() - started) * 1000,
            )
