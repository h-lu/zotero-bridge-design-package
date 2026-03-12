from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError


@dataclass(slots=True)
class BridgeError(Exception):
    code: str
    message: str
    status_code: int
    upstream_status: int | None = None
    request_id: str | None = None


def get_request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def error_payload(
    *,
    code: str,
    message: str,
    request_id: str | None,
    upstream_status: int | None = None,
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "upstreamStatus": upstream_status,
            "requestId": request_id,
        }
    }


async def bridge_error_handler(request: Request, exc: BridgeError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=error_payload(
            code=exc.code,
            message=exc.message,
            request_id=exc.request_id or get_request_id(request),
            upstream_status=exc.upstream_status,
        ),
    )


def _format_validation_message(exc: RequestValidationError | ValidationError) -> str:
    first_error = exc.errors()[0]
    location = ".".join(str(part) for part in first_error.get("loc", []) if part != "body")
    message = first_error.get("msg", "Validation error")
    return f"{location}: {message}" if location else message


def _is_missing_zotero_api_key_error(exc: RequestValidationError) -> bool:
    for error in exc.errors():
        if tuple(error.get("loc", ())) == ("header", "X-Zotero-API-Key") and error.get(
            "type"
        ) == "missing":
            return True
    return False


async def request_validation_error_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    if _is_missing_zotero_api_key_error(exc):
        return JSONResponse(
            status_code=401,
            content=error_payload(
                code="MISSING_ZOTERO_API_KEY",
                message="Missing X-Zotero-API-Key",
                request_id=get_request_id(request),
            ),
        )
    return JSONResponse(
        status_code=400,
        content=error_payload(
            code="BAD_REQUEST",
            message=_format_validation_message(exc),
            request_id=get_request_id(request),
        ),
    )


async def unexpected_error_handler(request: Request, _: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content=error_payload(
            code="INTERNAL_ERROR",
            message="Unexpected server error",
            request_id=get_request_id(request),
        ),
    )
